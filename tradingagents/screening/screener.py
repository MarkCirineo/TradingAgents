"""Ticker screener: generates a candidate universe from multiple sources.

Sources:
- **Alpaca most-actives**: ``ScreenerClient.get_most_actives()`` -- high-volume names.
- **Alpaca movers**: ``ScreenerClient.get_market_movers()`` -- top % gainers.
- **Yahoo Finance screener**: custom ``EquityQuery`` built from the swing
  playbook's own criteria (price range, liquidity floor, 52-week momentum).
  This is the widest net -- it scans the whole US market instead of a
  fixed top-N list.
- **Watchlist**: user-defined tickers from ``config["screening"]["watchlist"]``.
- **Hybrid**: merges all enabled sources, deduplicates (multi-source hits
  get a score bump), and ranks by composite score.

The output is a ranked list of ``ScreenerCandidate`` dicts that get passed
to ``pre_filter.py`` for quantitative filtering before entering the LLM
analysis pipeline.  The pre-filter remains the hard gatekeeper -- these
sources only decide *which* symbols are worth the API calls to check.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from tradingagents.default_config import DEFAULT_CONFIG

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Candidate data class
# ---------------------------------------------------------------------------

@dataclass
class ScreenerCandidate:
    """A ticker that passed the initial screening stage."""

    symbol: str
    source: str  # e.g. "alpaca_screener", "alpaca_movers", "yfinance_screener", "watchlist", or "a+b" when merged
    volume: int = 0
    trade_count: int = 0
    pct_change: float = 0.0  # day's percent change, when the source provides it
    score: float = 0.0
    reason: str = ""


# ---------------------------------------------------------------------------
# Source: Alpaca most-active screener
# ---------------------------------------------------------------------------

class AlpacaScreener:
    """Fetch the most-active tickers from Alpaca's screener endpoint."""

    def __init__(self, data_client=None):
        self._data_client = data_client

    @property
    def data_client(self):
        if self._data_client is None:
            from tradingagents.execution.alpaca_data import AlpacaDataClient
            self._data_client = AlpacaDataClient()
        return self._data_client

    # Alpaca caps the most-actives endpoint at 100 results — larger
    # requests are rejected outright ("invalid top").
    _API_MAX = 100

    def scan(self, top: int = 20) -> List[ScreenerCandidate]:
        """Return the top *top* most-active tickers by volume.

        Each candidate gets a normalised score based on its volume rank.
        *top* is clamped to the API maximum so a large ``max_candidates``
        doesn't error out this source entirely.
        """
        top = min(top, self._API_MAX)
        try:
            raw = self.data_client.get_most_active(top=top, by="volume")
        except Exception as exc:
            logger.error("Alpaca screener failed: %s", exc)
            return []

        candidates = []
        for rank, item in enumerate(raw, start=1):
            candidates.append(
                ScreenerCandidate(
                    symbol=item["symbol"],
                    source="alpaca_screener",
                    volume=item.get("volume", 0),
                    trade_count=item.get("trade_count", 0),
                    # Higher rank = higher score (rank 1 = top / top)
                    score=round((top - rank + 1) / top, 3),
                    reason=f"Most-active rank #{rank} by volume",
                )
            )
        logger.info("AlpacaScreener returned %d candidates", len(candidates))
        return candidates


# ---------------------------------------------------------------------------
# Source: Alpaca market movers (top % gainers)
# ---------------------------------------------------------------------------

class AlpacaMoversScreener:
    """Fetch the top percent-gainers from Alpaca's market-movers endpoint.

    Gainers fit the playbook's momentum profile (stocks in strong moves)
    far better than most-actives-by-volume, which surfaces mega-caps with
    2% ADR and sub-$5 volume churners.

    The raw gainers list is dominated by warrants and penny stocks, so
    results are filtered to the playbook's price band up front -- each
    discarded symbol saves ~9 market-data calls in the pre-filter.
    """

    def __init__(self, data_client=None, config: Optional[Dict[str, Any]] = None):
        self._data_client = data_client
        swing = (config or DEFAULT_CONFIG).get("swing_strategy", {})
        self._min_price = swing.get("min_price", 5.0)
        self._max_price = swing.get("max_price", 500.0)

    @property
    def data_client(self):
        if self._data_client is None:
            from tradingagents.execution.alpaca_data import AlpacaDataClient
            self._data_client = AlpacaDataClient()
        return self._data_client

    # Alpaca caps the movers endpoint at 50 results.
    _API_MAX = 50

    def scan(self, top: int = 20) -> List[ScreenerCandidate]:
        """Return up to *top* gainers within the playbook's price band.

        Over-fetches from the API (the raw gainers list is mostly
        warrants/pennies that the price band discards) so the filtered
        list still has close to *top* entries.
        """
        fetch = min(self._API_MAX, max(top * 3, 30))
        try:
            raw = self.data_client.get_market_movers(top=fetch)
        except Exception as exc:
            logger.error("Alpaca movers screener failed: %s", exc)
            return []

        in_band = [
            item for item in raw
            if self._min_price <= item.get("price", 0.0) <= self._max_price
        ][:top]

        candidates = []
        for rank, item in enumerate(in_band, start=1):
            pct = item.get("percent_change", 0.0)
            candidates.append(
                ScreenerCandidate(
                    symbol=item["symbol"],
                    source="alpaca_movers",
                    pct_change=pct,
                    score=round((top - rank + 1) / top, 3),
                    reason=f"Top gainer rank #{rank} ({pct:+.1f}%)",
                )
            )
        logger.info(
            "AlpacaMoversScreener returned %d candidates (%d/%d filtered by price band)",
            len(candidates), len(raw) - len(in_band), len(raw),
        )
        return candidates


# ---------------------------------------------------------------------------
# Source: Yahoo Finance custom screener (criteria-based, whole-market)
# ---------------------------------------------------------------------------

class YFinanceScreener:
    """Scan the whole US market via Yahoo Finance's screener API.

    Unlike the Alpaca endpoints (fixed top-N lists), this runs a custom
    ``EquityQuery`` built from the swing playbook's own selection
    criteria, so every result is already roughly the right shape:

    - Price within the playbook's ``min_price``-``max_price`` band
    - 3-month average volume above a liquidity floor
    - 52-week percent change above a momentum floor (top-of-funnel proxy
      for the "30%+ prior uptrend" rule -- the pre-filter enforces the
      real 60-day version)
    - Listed on major US exchanges, common stock only (no ETFs/funds)

    Yahoo caps each request at 250 rows; results are paginated up to
    ``max_results``.  No API key required.
    """

    # Yahoo caps the screener page size at 250 rows per request.
    _PAGE_SIZE_CAP = 250

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        cfg = config or DEFAULT_CONFIG
        screening_cfg = cfg.get("screening", {})
        self._cfg = screening_cfg.get("yfinance_screener", {})
        swing = cfg.get("swing_strategy", {})
        self._min_price = swing.get("min_price", 5.0)
        self._max_price = swing.get("max_price", 500.0)
        self._max_results = int(self._cfg.get("max_results", 100))
        self._min_avg_volume = int(self._cfg.get("min_avg_volume_3m", 500_000))
        self._min_52wk_change = float(self._cfg.get("min_52wk_change_pct", 30.0))
        self._exchanges = list(self._cfg.get("exchanges", ["NMS", "NYQ", "NGM", "ASE"]))
        self._sort_field = self._cfg.get("sort_field", "percentchange")

    def _build_query(self):
        from yfinance import EquityQuery

        return EquityQuery(
            "and",
            [
                EquityQuery("is-in", ["exchange", *self._exchanges]),
                EquityQuery("btwn", ["intradayprice", self._min_price, self._max_price]),
                EquityQuery("gt", ["avgdailyvol3m", self._min_avg_volume]),
                EquityQuery("gt", ["fiftytwowkpercentchange", self._min_52wk_change]),
            ],
        )

    @staticmethod
    def _normalize_symbol(symbol: str) -> str:
        # Yahoo uses dashes for share classes (BRK-B); Alpaca uses dots.
        return symbol.strip().upper().replace("-", ".")

    def scan(self) -> List[ScreenerCandidate]:
        """Run the custom query and return rank-scored candidates."""
        try:
            import yfinance as yf
        except ImportError:
            logger.error("yfinance not installed -- YFinanceScreener disabled")
            return []

        query = self._build_query()
        quotes: List[dict] = []
        offset = 0
        while len(quotes) < self._max_results:
            size = min(self._PAGE_SIZE_CAP, self._max_results - len(quotes))
            try:
                response = yf.screen(
                    query,
                    offset=offset,
                    size=size,
                    sortField=self._sort_field,
                    sortAsc=False,
                )
            except Exception as exc:
                logger.error("yfinance screen failed at offset %d: %s", offset, exc)
                break
            page = (response or {}).get("quotes", [])
            if not page:
                break
            quotes.extend(page)
            offset += len(page)
            total = (response or {}).get("total", 0)
            if offset >= total:
                break

        candidates = []
        seen = set()
        for quote in quotes:
            symbol = quote.get("symbol", "")
            if not symbol or quote.get("quoteType") != "EQUITY":
                continue
            symbol = self._normalize_symbol(symbol)
            if symbol in seen:
                continue
            seen.add(symbol)
            rank = len(candidates) + 1
            pct = float(quote.get("regularMarketChangePercent") or 0.0)
            wk52 = float(quote.get("fiftyTwoWeekChangePercent") or 0.0)
            candidates.append(
                ScreenerCandidate(
                    symbol=symbol,
                    source="yfinance_screener",
                    volume=int(quote.get("regularMarketVolume") or 0),
                    pct_change=pct,
                    score=round((self._max_results - rank + 1) / self._max_results, 3),
                    reason=(
                        f"YF screen rank #{rank} "
                        f"(day {pct:+.1f}%, 52wk {wk52:+.0f}%)"
                    ),
                )
            )

        logger.info("YFinanceScreener returned %d candidates", len(candidates))
        return candidates


# ---------------------------------------------------------------------------
# Source: user-defined watchlist
# ---------------------------------------------------------------------------

class WatchlistScreener:
    """Generate candidates from a static user-defined watchlist."""

    def __init__(self, watchlist: Optional[List[str]] = None, config: Optional[Dict] = None):
        cfg = config or DEFAULT_CONFIG
        self._watchlist = watchlist or cfg.get("screening", {}).get("watchlist", [])

    def scan(self) -> List[ScreenerCandidate]:
        """Return all watchlist tickers as candidates with a fixed score."""
        candidates = []
        for symbol in self._watchlist:
            candidates.append(
                ScreenerCandidate(
                    symbol=symbol.upper().strip(),
                    source="watchlist",
                    score=0.80,  # Fixed score -- watchlist items are pre-vetted
                    reason="User watchlist",
                )
            )
        logger.info("WatchlistScreener returned %d candidates", len(candidates))
        return candidates


# ---------------------------------------------------------------------------
# Hybrid: merge, deduplicate, rank
# ---------------------------------------------------------------------------

class HybridScreener:
    """Merge candidates from all enabled screening sources.

    ``config["screening"]["source"]`` selects the source group:

    - ``"alpaca"``:    Alpaca most-actives + movers (per sub-flags)
    - ``"yfinance"``:  Yahoo Finance criteria screener only
    - ``"watchlist"``: user watchlist only
    - ``"hybrid"``:    all enabled sources merged (default)

    A symbol surfaced by multiple sources keeps its best score plus a
    +0.15 bump per extra source -- independent signals agreeing is
    itself a signal.

    Parameters
    ----------
    data_client : AlpacaDataClient, optional
        Shared data client (avoids creating multiple connections).
    config : dict, optional
        Configuration dictionary.  Defaults to ``DEFAULT_CONFIG``.
    """

    _MERGE_BUMP = 0.15

    def __init__(
        self,
        data_client=None,
        config: Optional[Dict[str, Any]] = None,
    ):
        self._config = config or DEFAULT_CONFIG
        screening_cfg = self._config.get("screening", {})
        self._source = screening_cfg.get("source", "hybrid")
        self._max_candidates = screening_cfg.get("max_candidates", 20)
        self._use_most_actives = screening_cfg.get("alpaca_most_actives", True)
        self._use_movers = screening_cfg.get("alpaca_movers", True)
        self._use_yfinance = screening_cfg.get("yfinance_screener", {}).get(
            "enabled", True
        )

        self._alpaca = AlpacaScreener(data_client=data_client)
        self._movers = AlpacaMoversScreener(data_client=data_client, config=self._config)
        self._yfinance = YFinanceScreener(config=self._config)
        self._watchlist = WatchlistScreener(config=self._config)

    def _merge(
        self,
        candidates: Dict[str, ScreenerCandidate],
        new: List[ScreenerCandidate],
    ) -> None:
        """Merge *new* candidates into *candidates* in place."""
        for c in new:
            existing = candidates.get(c.symbol)
            if existing is None:
                candidates[c.symbol] = c
                continue
            existing.score = min(1.0, max(existing.score, c.score) + self._MERGE_BUMP)
            existing.source = f"{existing.source}+{c.source}"
            existing.reason = f"{existing.reason} + {c.reason}"
            existing.volume = max(existing.volume, c.volume)
            existing.trade_count = max(existing.trade_count, c.trade_count)
            if c.pct_change:
                existing.pct_change = c.pct_change

    def scan(self) -> List[ScreenerCandidate]:
        """Run the configured screening sources and return merged candidates.

        Returns a deduplicated, ranked list capped at ``max_candidates``.
        """
        candidates: Dict[str, ScreenerCandidate] = {}

        # --- Alpaca sources ---
        if self._source in ("alpaca", "hybrid"):
            if self._use_most_actives:
                self._merge(candidates, self._alpaca.scan(top=self._max_candidates))
            if self._use_movers:
                self._merge(candidates, self._movers.scan(top=self._max_candidates))

        # --- Yahoo Finance criteria screener ---
        if self._source in ("yfinance", "hybrid") and self._use_yfinance:
            self._merge(candidates, self._yfinance.scan())

        # --- Watchlist ---
        if self._source in ("watchlist", "hybrid"):
            self._merge(candidates, self._watchlist.scan())

        # Sort by score descending, cap at max
        result = sorted(candidates.values(), key=lambda c: c.score, reverse=True)
        result = result[: self._max_candidates]

        logger.info(
            "HybridScreener: %d candidates (source=%s)", len(result), self._source
        )
        return result
