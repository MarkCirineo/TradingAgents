"""Tests for the batched pre-filter in screening/pre_filter.py.

Validates:
  - All checks compute off a single batched bars fetch (1 API call for
    N symbols instead of ~9 per symbol)
  - Check semantics match the old per-check fetch behavior
  - Fallback to per-symbol fetch when a symbol is missing from the batch
  - A synthetic textbook setup passes every gate end-to-end
"""

import numpy as np
import pandas as pd
import pytest

from tradingagents.execution.alpaca_data import AlpacaDataClient
from tradingagents.screening.pre_filter import PreFilter


# ---------------------------------------------------------------------------
# Synthetic bar builders
# ---------------------------------------------------------------------------

def _make_frame(rows):
    idx = pd.date_range("2026-03-02", periods=len(rows), freq="B", name="timestamp")
    return pd.DataFrame(rows, index=idx)


def _passing_bars():
    """A textbook playbook setup: 60-day pole then 20-day tightening flag.

    - Prior uptrend: 20 -> 56 ramp (well above +30%)
    - ADR ~5-7% during the move, price $60 (in the $5-500 band)
    - Dollar volume ~$60M (above $50M)
    - RS vs flat SPY: consolidation drifts 56 -> 60 = +7% over 20 days
    - MA stack: 10 > 20 > 50 (still rising)
    - Last 5 days: tight ranges (~1.5% vs 7% ADR) + contracting volume
    """
    rows = []
    for i in range(60):  # the pole
        close = 20 + (56 - 20) * i / 59
        rng = close * 0.07
        rows.append(dict(
            open=close, high=close + rng / 2, low=close - rng / 2,
            close=close, volume=1_200_000,
        ))
    for i in range(20):  # the flag
        close = 56 + 4 * i / 19
        rng = close * (0.015 if i >= 14 else 0.07)
        vol = 500_000 if i >= 15 else 1_200_000
        rows.append(dict(
            open=close, high=close + rng / 2, low=close - rng / 2,
            close=close, volume=vol,
        ))
    return _make_frame(rows)


def _flat_spy_bars():
    rows = [
        dict(open=500.0, high=502.0, low=498.0, close=500.0, volume=50_000_000)
        for _ in range(80)
    ]
    return _make_frame(rows)


# ---------------------------------------------------------------------------
# Stub data client
# ---------------------------------------------------------------------------

class StubDataClient:
    """Serves canned frames and records every get_bars call."""

    def __init__(self, frames):
        self.frames = frames  # symbol -> DataFrame
        self.calls = []

    def get_bars(self, symbols, timeframe=None, start=None, end=None, lookback_days=60):
        if isinstance(symbols, str):
            symbols = [symbols]
        self.calls.append(list(symbols))
        parts = []
        for sym in symbols:
            df = self.frames.get(sym)
            if df is None or df.empty:
                continue
            part = df.copy()
            part["symbol"] = sym
            part = part.set_index("symbol", append=True).reorder_levels(
                ["symbol", "timestamp"]
            )
            parts.append(part)
        if not parts:
            return pd.DataFrame()
        return pd.concat(parts)

    # Pure computation -- reuse the real implementation (ignores self
    # when bars are supplied, which the pre-filter always does).
    def compute_consolidation_pivot(self, symbol, bars=None, **kwargs):
        return AlpacaDataClient.compute_consolidation_pivot(
            self, symbol, bars=bars, **kwargs
        )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestBatchedFetch:
    def test_one_api_call_for_many_symbols(self):
        frames = {
            "AAA": _passing_bars(),
            "BBB": _passing_bars(),
            "CCC": _passing_bars(),
            "SPY": _flat_spy_bars(),
        }
        client = StubDataClient(frames)
        pf = PreFilter(data_client=client)
        results = pf.evaluate_all(["AAA", "BBB", "CCC"])

        assert len(results) == 3
        assert len(client.calls) == 1
        assert set(client.calls[0]) == {"AAA", "BBB", "CCC", "SPY"}

    def test_missing_symbol_falls_back_to_single_fetch(self):
        client = StubDataClient({"AAA": _passing_bars(), "SPY": _flat_spy_bars()})
        pf = PreFilter(data_client=client)
        results = pf.evaluate_all(["AAA", "GONE"])

        gone = next(r for r in results if r.symbol == "GONE")
        assert not gone.passed
        assert "no price data" in gone.reject_reason
        # 1 batch call + 1 single-symbol fallback for GONE
        assert client.calls == [["AAA", "GONE", "SPY"], ["GONE"]]

    def test_spy_fetched_once_when_missing_from_batch(self):
        client = StubDataClient({"AAA": _passing_bars(), "BBB": _passing_bars()})
        pf = PreFilter(data_client=client)
        pf.evaluate_all(["AAA", "BBB"])

        spy_calls = [c for c in client.calls if c == ["SPY"]]
        assert len(spy_calls) == 1


class TestCheckSemantics:
    def _run_one(self, bars, spy_bars=None):
        client = StubDataClient({
            "AAA": bars, "SPY": spy_bars if spy_bars is not None else _flat_spy_bars(),
        })
        pf = PreFilter(data_client=client)
        return pf.evaluate_all(["AAA"])[0]

    def test_textbook_setup_passes_all_gates(self):
        result = self._run_one(_passing_bars())
        assert result.passed, f"rejected: {result.reject_reason}"
        ch = result.checks
        assert ch["dollar_volume_value"] > 50_000_000
        assert ch["adr_pct_value"] >= 0.04
        assert ch["relative_strength_value"] >= 0.05
        assert ch["prior_uptrend_value"] >= 0.30
        assert ch["tight_consolidation"] is True
        assert ch["pivot_high"] > ch["pivot_low"] > 0
        assert result.score > 0

    def test_low_dollar_volume_rejected(self):
        bars = _passing_bars()
        bars["volume"] = 10_000  # ~$600k/day
        result = self._run_one(bars)
        assert not result.passed
        assert "dollar volume" in result.reject_reason

    def test_low_adr_rejected(self):
        bars = _passing_bars()
        # Squeeze all ranges to ~1% of close
        mid = (bars["high"] + bars["low"]) / 2
        bars["high"] = mid * 1.005
        bars["low"] = mid * 0.995
        result = self._run_one(bars)
        assert not result.passed
        assert "ADR" in result.reject_reason

    def test_rs_compares_against_spy(self):
        # SPY rallying as hard as the symbol -> no outperformance
        spy = _passing_bars()
        result = self._run_one(_passing_bars(), spy_bars=spy)
        assert not result.passed
        assert "RS" in result.reject_reason

    def test_flat_stock_fails_uptrend_and_ma_stack(self):
        rows = [
            dict(open=50.0, high=52.0, low=48.0, close=50.0, volume=2_000_000)
            for _ in range(80)
        ]
        result = self._run_one(_make_frame(rows))
        assert not result.passed
        assert "prior uptrend" in result.reject_reason
        assert "MAs not stacked" in result.reject_reason

    def test_insufficient_history_rejected(self):
        result = self._run_one(_passing_bars().tail(30))
        assert not result.passed
        assert "insufficient data for uptrend check" in result.reject_reason

    def test_already_held_rejected(self):
        class StubDB:
            def get_open_positions(self, include_pending=False):
                return [{"symbol": "AAA"}]

        client = StubDataClient({"AAA": _passing_bars(), "SPY": _flat_spy_bars()})
        pf = PreFilter(data_client=client, trade_db=StubDB())
        result = pf.evaluate_all(["AAA"])[0]
        assert not result.passed
        assert "already holding" in result.reject_reason


class TestFilterCandidatesContract:
    def test_returns_only_passing_sorted_by_score(self):
        weak = _passing_bars()
        weak["volume"] = 10_000  # fails dollar volume
        frames = {
            "GOOD": _passing_bars(),
            "WEAK": weak,
            "SPY": _flat_spy_bars(),
        }
        pf = PreFilter(data_client=StubDataClient(frames))
        results = pf.filter_candidates(["WEAK", "GOOD"])

        assert [r.symbol for r in results] == ["GOOD"]
        assert results[0].passed
