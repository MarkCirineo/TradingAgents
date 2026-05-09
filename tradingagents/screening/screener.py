"""Ticker screener: generates a candidate universe from multiple sources.

Sources:
- **Alpaca screener**: ``ScreenerClient.get_most_actives()`` -- high-volume movers.
- **Watchlist**: user-defined tickers from ``config["screening"]["watchlist"]``.
- **Hybrid**: merges both, deduplicates, and ranks by a composite score.

The output is a ranked list of ``ScreenerCandidate`` dicts that get passed
to ``pre_filter.py`` for quantitative filtering before entering the LLM
analysis pipeline.
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
    source: str  # "alpaca_screener", "watchlist", or "both"
    volume: int = 0
    trade_count: int = 0
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

    def scan(self, top: int = 20) -> List[ScreenerCandidate]:
        """Return the top *top* most-active tickers by volume.

        Each candidate gets a normalised score based on its volume rank.
        """
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
    """Merge Alpaca screener and watchlist results.

    Parameters
    ----------
    data_client : AlpacaDataClient, optional
        Shared data client (avoids creating multiple connections).
    config : dict, optional
        Configuration dictionary.  Defaults to ``DEFAULT_CONFIG``.
    """

    def __init__(
        self,
        data_client=None,
        config: Optional[Dict[str, Any]] = None,
    ):
        self._config = config or DEFAULT_CONFIG
        screening_cfg = self._config.get("screening", {})
        self._source = screening_cfg.get("source", "alpaca")
        self._max_candidates = screening_cfg.get("max_candidates", 20)

        self._alpaca = AlpacaScreener(data_client=data_client)
        self._watchlist = WatchlistScreener(config=self._config)

    def scan(self) -> List[ScreenerCandidate]:
        """Run the configured screening sources and return merged candidates.

        Returns a deduplicated, ranked list capped at ``max_candidates``.
        """
        candidates: Dict[str, ScreenerCandidate] = {}

        # --- Alpaca screener ---
        if self._source in ("alpaca", "hybrid"):
            for c in self._alpaca.scan(top=self._max_candidates):
                candidates[c.symbol] = c

        # --- Watchlist ---
        if self._source in ("watchlist", "hybrid"):
            for c in self._watchlist.scan():
                if c.symbol in candidates:
                    # Merge: bump score, mark source as "both"
                    existing = candidates[c.symbol]
                    existing.score = min(1.0, existing.score + 0.20)
                    existing.source = "both"
                    existing.reason += " + User watchlist"
                else:
                    candidates[c.symbol] = c

        # Sort by score descending, cap at max
        result = sorted(candidates.values(), key=lambda c: c.score, reverse=True)
        result = result[: self._max_candidates]

        logger.info(
            "HybridScreener: %d candidates (source=%s)", len(result), self._source
        )
        return result
