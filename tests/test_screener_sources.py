"""Tests for the multi-source ticker screener in screening/screener.py.

Validates:
  - YFinanceScreener: quote parsing, EQUITY-only filtering, symbol
    normalization, deduplication, rank scoring, pagination
  - AlpacaMoversScreener: gainer parsing and rank scoring
  - HybridScreener: source selection, merge/dedup with score bumps,
    max_candidates cap
"""

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from tradingagents.screening.screener import (
    AlpacaMoversScreener,
    AlpacaScreener,
    HybridScreener,
    ScreenerCandidate,
    YFinanceScreener,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_config(**screening_overrides):
    """Build a minimal config dict for screener construction."""
    screening = {
        "source": "hybrid",
        "watchlist": [],
        "max_candidates": 35,
        "alpaca_most_actives": True,
        "alpaca_movers": True,
        "yfinance_screener": {
            "enabled": True,
            "max_results": 100,
            "min_avg_volume_3m": 500_000,
            "min_52wk_change_pct": 30.0,
            "exchanges": ["NMS", "NYQ"],
            "sort_field": "percentchange",
        },
    }
    screening.update(screening_overrides)
    return {
        "screening": screening,
        "swing_strategy": {"min_price": 5.0, "max_price": 500.0},
    }


def _yf_quote(symbol, quote_type="EQUITY", pct=2.5, wk52=80.0, volume=1_000_000):
    return {
        "symbol": symbol,
        "quoteType": quote_type,
        "regularMarketChangePercent": pct,
        "fiftyTwoWeekChangePercent": wk52,
        "regularMarketVolume": volume,
    }


class _StubSource:
    """Stands in for any sub-screener inside HybridScreener."""

    def __init__(self, candidates):
        self._candidates = candidates
        self.called = False

    def scan(self, **kwargs):
        self.called = True
        return list(self._candidates)


# ---------------------------------------------------------------------------
# YFinanceScreener
# ---------------------------------------------------------------------------

class TestYFinanceScreener:
    def _scan_with_pages(self, screener, pages, total):
        """Run scan() with yf.screen returning successive canned pages."""
        responses = [{"quotes": page, "total": total} for page in pages]

        def fake_screen(query, offset, size, sortField, sortAsc):
            if not responses:
                return {"quotes": [], "total": total}
            return responses.pop(0)

        with patch("yfinance.screen", side_effect=fake_screen):
            return screener.scan()

    def test_parses_quotes_and_rank_scores(self):
        screener = YFinanceScreener(config=_make_config())
        quotes = [_yf_quote("AAA"), _yf_quote("BBB"), _yf_quote("CCC")]
        result = self._scan_with_pages(screener, [quotes], total=3)

        assert [c.symbol for c in result] == ["AAA", "BBB", "CCC"]
        assert all(c.source == "yfinance_screener" for c in result)
        # Rank 1 of max_results=100 -> (100 - 1 + 1) / 100 = 1.0
        assert result[0].score == 1.0
        assert result[1].score == 0.99
        assert result[0].pct_change == 2.5
        assert result[0].volume == 1_000_000

    def test_filters_non_equity_quotes(self):
        screener = YFinanceScreener(config=_make_config())
        quotes = [
            _yf_quote("AAA"),
            _yf_quote("SPY", quote_type="ETF"),
            _yf_quote("SOMEFUND", quote_type="MUTUALFUND"),
        ]
        result = self._scan_with_pages(screener, [quotes], total=3)
        assert [c.symbol for c in result] == ["AAA"]

    def test_normalizes_share_class_symbols(self):
        screener = YFinanceScreener(config=_make_config())
        result = self._scan_with_pages(screener, [[_yf_quote("BRK-B")]], total=1)
        assert result[0].symbol == "BRK.B"

    def test_deduplicates_symbols(self):
        screener = YFinanceScreener(config=_make_config())
        quotes = [_yf_quote("AAA"), _yf_quote("AAA"), _yf_quote("BBB")]
        result = self._scan_with_pages(screener, [quotes], total=3)
        assert [c.symbol for c in result] == ["AAA", "BBB"]

    def test_paginates_until_max_results(self):
        cfg = _make_config()
        cfg["screening"]["yfinance_screener"]["max_results"] = 4
        screener = YFinanceScreener(config=cfg)
        pages = [
            [_yf_quote("AAA"), _yf_quote("BBB")],
            [_yf_quote("CCC"), _yf_quote("DDD")],
            [_yf_quote("EEE")],  # must never be requested
        ]
        result = self._scan_with_pages(screener, pages, total=10)
        assert [c.symbol for c in result] == ["AAA", "BBB", "CCC", "DDD"]

    def test_stops_when_total_exhausted(self):
        screener = YFinanceScreener(config=_make_config())
        result = self._scan_with_pages(screener, [[_yf_quote("AAA")]], total=1)
        assert len(result) == 1

    def test_returns_empty_on_api_error(self):
        screener = YFinanceScreener(config=_make_config())
        with patch("yfinance.screen", side_effect=RuntimeError("boom")):
            assert screener.scan() == []

    def test_missing_market_fields_default_to_zero(self):
        screener = YFinanceScreener(config=_make_config())
        quote = {"symbol": "AAA", "quoteType": "EQUITY",
                 "regularMarketChangePercent": None}
        result = self._scan_with_pages(screener, [[quote]], total=1)
        assert result[0].pct_change == 0.0
        assert result[0].volume == 0

    def test_build_query_uses_playbook_params(self):
        cfg = _make_config()
        cfg["swing_strategy"] = {"min_price": 10.0, "max_price": 200.0}
        screener = YFinanceScreener(config=cfg)
        query_dict = screener._build_query().to_dict()
        assert query_dict["operator"] == "AND"
        flat = str(query_dict)
        assert "10.0" in flat and "200.0" in flat
        assert "avgdailyvol3m" in flat
        assert "fiftytwowkpercentchange" in flat


# ---------------------------------------------------------------------------
# AlpacaScreener (most actives)
# ---------------------------------------------------------------------------

class TestAlpacaScreener:
    def test_clamps_top_to_api_max(self):
        """Alpaca rejects top > 100 — a large max_candidates must not
        error out the whole source (it would silently return [])."""
        requested = {}

        def fake_most_active(top, by="volume"):
            requested["top"] = top
            return [{"symbol": "AAA", "volume": 1_000_000, "trade_count": 5000}]

        screener = AlpacaScreener(
            data_client=SimpleNamespace(get_most_active=fake_most_active)
        )
        result = screener.scan(top=300)

        assert requested["top"] == 100
        assert len(result) == 1
        # Rank score normalised by the clamped count
        assert result[0].score == 1.0


# ---------------------------------------------------------------------------
# AlpacaMoversScreener
# ---------------------------------------------------------------------------

class TestAlpacaMoversScreener:
    def test_parses_gainers_and_rank_scores(self):
        stub_client = SimpleNamespace(
            get_market_movers=lambda top: [
                {"symbol": "AAA", "price": 12.0, "change": 2.0, "percent_change": 20.0},
                {"symbol": "BBB", "price": 30.0, "change": 3.0, "percent_change": 11.0},
            ]
        )
        screener = AlpacaMoversScreener(data_client=stub_client, config=_make_config())
        result = screener.scan(top=2)

        assert [c.symbol for c in result] == ["AAA", "BBB"]
        assert all(c.source == "alpaca_movers" for c in result)
        assert result[0].score == 1.0
        assert result[0].pct_change == 20.0
        assert "+20.0%" in result[0].reason

    def test_filters_out_of_price_band(self):
        """Warrants/penny-stock gainers outside $5-$500 must be dropped."""
        stub_client = SimpleNamespace(
            get_market_movers=lambda top: [
                {"symbol": "PENNY", "price": 0.42, "change": 0.3, "percent_change": 223.0},
                {"symbol": "GOOD", "price": 25.0, "change": 4.0, "percent_change": 19.0},
                {"symbol": "PRICY", "price": 900.0, "change": 90.0, "percent_change": 11.0},
            ]
        )
        screener = AlpacaMoversScreener(data_client=stub_client, config=_make_config())
        result = screener.scan(top=3)
        assert [c.symbol for c in result] == ["GOOD"]
        # Score reflects rank within the filtered (in-band) list
        assert result[0].score == 1.0

    def test_overfetches_to_survive_filtering(self):
        """scan(top=N) must request more than N raw movers from the API."""
        requested = {}

        def fake_movers(top):
            requested["top"] = top
            return []

        screener = AlpacaMoversScreener(
            data_client=SimpleNamespace(get_market_movers=fake_movers),
            config=_make_config(),
        )
        screener.scan(top=10)
        assert requested["top"] == 30

        screener.scan(top=50)
        assert requested["top"] == 50  # capped at Alpaca's API max

    def test_returns_empty_on_api_error(self):
        def _boom(top):
            raise RuntimeError("api down")

        screener = AlpacaMoversScreener(
            data_client=SimpleNamespace(get_market_movers=_boom),
            config=_make_config(),
        )
        assert screener.scan() == []


# ---------------------------------------------------------------------------
# HybridScreener
# ---------------------------------------------------------------------------

def _hybrid_with_stubs(config, actives=(), movers=(), yfinance=(), watchlist=()):
    """Build a HybridScreener with all sub-sources stubbed out."""
    screener = HybridScreener(config=config)
    screener._alpaca = _StubSource(actives)
    screener._movers = _StubSource(movers)
    screener._yfinance = _StubSource(yfinance)
    screener._watchlist = _StubSource(watchlist)
    return screener


def _cand(symbol, source, score, **kwargs):
    return ScreenerCandidate(symbol=symbol, source=source, score=score, **kwargs)


class TestHybridScreener:
    def test_merges_all_sources_in_hybrid_mode(self):
        screener = _hybrid_with_stubs(
            _make_config(),
            actives=[_cand("AAA", "alpaca_screener", 0.9)],
            movers=[_cand("BBB", "alpaca_movers", 0.8)],
            yfinance=[_cand("CCC", "yfinance_screener", 0.7)],
            watchlist=[_cand("DDD", "watchlist", 0.8)],
        )
        symbols = {c.symbol for c in screener.scan()}
        assert symbols == {"AAA", "BBB", "CCC", "DDD"}

    def test_multi_source_hit_gets_score_bump(self):
        screener = _hybrid_with_stubs(
            _make_config(),
            actives=[_cand("AAA", "alpaca_screener", 0.5, volume=100)],
            movers=[_cand("AAA", "alpaca_movers", 0.7, pct_change=15.0)],
        )
        result = screener.scan()
        assert len(result) == 1
        merged = result[0]
        # max(0.5, 0.7) + 0.15 bump
        assert merged.score == pytest.approx(0.85)
        assert merged.source == "alpaca_screener+alpaca_movers"
        assert merged.pct_change == 15.0
        assert merged.volume == 100

    def test_score_bump_caps_at_one(self):
        screener = _hybrid_with_stubs(
            _make_config(),
            actives=[_cand("AAA", "alpaca_screener", 0.95)],
            movers=[_cand("AAA", "alpaca_movers", 0.9)],
        )
        assert screener.scan()[0].score == 1.0

    def test_respects_max_candidates_cap(self):
        cfg = _make_config(max_candidates=2)
        screener = _hybrid_with_stubs(
            cfg,
            yfinance=[
                _cand("AAA", "yfinance_screener", 0.9),
                _cand("BBB", "yfinance_screener", 0.8),
                _cand("CCC", "yfinance_screener", 0.7),
            ],
        )
        result = screener.scan()
        assert [c.symbol for c in result] == ["AAA", "BBB"]

    def test_source_alpaca_skips_yfinance_and_watchlist(self):
        screener = _hybrid_with_stubs(
            _make_config(source="alpaca"),
            actives=[_cand("AAA", "alpaca_screener", 0.9)],
            yfinance=[_cand("CCC", "yfinance_screener", 0.7)],
            watchlist=[_cand("DDD", "watchlist", 0.8)],
        )
        assert {c.symbol for c in screener.scan()} == {"AAA"}
        assert not screener._yfinance.called
        assert not screener._watchlist.called

    def test_source_yfinance_skips_alpaca(self):
        screener = _hybrid_with_stubs(
            _make_config(source="yfinance"),
            actives=[_cand("AAA", "alpaca_screener", 0.9)],
            yfinance=[_cand("CCC", "yfinance_screener", 0.7)],
        )
        assert {c.symbol for c in screener.scan()} == {"CCC"}
        assert not screener._alpaca.called
        assert not screener._movers.called

    def test_disabled_sub_sources_are_skipped(self):
        cfg = _make_config(
            alpaca_most_actives=False,
            alpaca_movers=False,
            yfinance_screener={"enabled": False},
        )
        screener = _hybrid_with_stubs(
            cfg,
            actives=[_cand("AAA", "alpaca_screener", 0.9)],
            movers=[_cand("BBB", "alpaca_movers", 0.8)],
            yfinance=[_cand("CCC", "yfinance_screener", 0.7)],
            watchlist=[_cand("DDD", "watchlist", 0.8)],
        )
        assert {c.symbol for c in screener.scan()} == {"DDD"}
