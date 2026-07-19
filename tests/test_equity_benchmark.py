"""Tests for the SPY / blended benchmark on the equity curve.

Both benchmarks rebase to the account's starting equity so they share a
dollar axis with the equity line and start together; the summary reports
your return, the benchmark return, and the delta (alpha) over the window.
"""

import pandas as pd
import pytest

from tradingagents.dashboard.api import snapshots as snap


class _FakeData:
    """Serves canned SPY daily closes indexed by date."""

    def __init__(self, dates, closes):
        self._df = pd.DataFrame({"close": closes}, index=pd.to_datetime(dates))

    def get_bars(self, symbol, start=None, end=None, **kw):
        return self._df


@pytest.fixture
def patch_ctx(monkeypatch):
    def _apply(data_client, cfg=None):
        monkeypatch.setattr(
            "tradingagents.dashboard.app.get_data_client", lambda: data_client
        )
        monkeypatch.setattr(
            "tradingagents.dashboard.app.get_config",
            lambda: cfg or {"guardrails": {"max_exposure_pct": 0.60}},
        )
    return _apply


def _equity(dates, values):
    return [{"time": d, "value": float(v)} for d, v in zip(dates, values)]


def test_spy_rebases_and_computes_delta(patch_ctx):
    dates = ["2026-07-01", "2026-07-02", "2026-07-11"]
    patch_ctx(_FakeData(dates, [400.0, 405.0, 420.0]))   # SPY +5%
    series = _equity(dates, [100.0, 101.0, 110.0])       # account +10%

    out = snap._benchmark_series(series, "spy")

    assert out is not None
    bench = out["series"]
    assert bench[0]["value"] == pytest.approx(100.0)     # rebased to start equity
    assert bench[-1]["value"] == pytest.approx(105.0)    # 100 x 420/400
    s = out["summary"]
    assert s["your_return"] == pytest.approx(0.10)
    assert s["benchmark_return"] == pytest.approx(0.05)
    assert s["delta"] == pytest.approx(0.05)             # beating SPY by 5pts


def test_blend_adds_cash_yield(patch_ctx):
    dates = ["2026-07-01", "2026-07-11"]
    patch_ctx(_FakeData(dates, [400.0, 420.0]))
    series = _equity(dates, [100.0, 110.0])

    out = snap._benchmark_series(series, "blend")

    bench = out["series"]
    cash = (1.0 + snap.CASH_APY) ** (10 / 365.0)          # 40% at ~3.3% APY
    expected_end = 100.0 * (0.60 * (420 / 400) + 0.40 * cash)
    assert bench[0]["value"] == pytest.approx(100.0)
    assert bench[-1]["value"] == pytest.approx(expected_end, abs=0.01)
    # Blend trails full SPY in an up market (cash drag), as designed.
    assert out["summary"]["benchmark_return"] < 0.05


def test_blend_weight_tracks_configured_exposure(patch_ctx):
    dates = ["2026-07-01", "2026-07-11"]
    patch_ctx(
        _FakeData(dates, [400.0, 440.0]),                 # SPY +10%
        cfg={"guardrails": {"max_exposure_pct": 0.80}},   # 80/20 blend
    )
    series = _equity(dates, [100.0, 100.0])

    out = snap._benchmark_series(series, "blend")
    cash = (1.0 + snap.CASH_APY) ** (10 / 365.0)
    expected_end = 100.0 * (0.80 * (440 / 400) + 0.20 * cash)
    assert out["series"][-1]["value"] == pytest.approx(expected_end, abs=0.01)


def test_no_data_client_returns_none(patch_ctx):
    patch_ctx(None)
    series = _equity(["2026-07-01", "2026-07-02"], [100.0, 101.0])
    assert snap._benchmark_series(series, "spy") is None


def test_carry_forward_when_spy_missing_a_date(patch_ctx):
    # An equity snapshot on a date with no SPY bar carries the prior close.
    patch_ctx(_FakeData(["2026-07-01", "2026-07-03"], [400.0, 404.0]))
    series = _equity(["2026-07-01", "2026-07-02", "2026-07-03"], [100, 100, 100])

    out = snap._benchmark_series(series, "spy")

    assert out is not None
    assert len(out["series"]) == 3            # midpoint uses carried-forward SPY
    assert out["series"][1]["value"] == pytest.approx(100.0)  # 100 x 400/400
