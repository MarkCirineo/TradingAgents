"""Sector lookup for portfolio concentration limits.

The swing playbook deliberately concentrates in momentum leaders, and
momentum leaders cluster into themes.  The expert document flags this
directly: "Be aware of total exposure to ... a single sector/theme
(peoplewish example: 3x10% AI positions = 30% sector risk)."

``get_sector()`` backs the sector guardrail in ``guardrails.py``.  It is
deliberately **fail-open**: a Yahoo Finance outage must not halt trading,
so an unresolved symbol returns ``None`` and the caller skips the check
rather than blocking the entry.
"""

from __future__ import annotations

import logging
import threading
from typing import Dict, Optional

logger = logging.getLogger(__name__)

# Sector membership effectively never changes, so the cache has no TTL --
# it lives for the daemon process.  Negative results are cached too, to
# avoid re-hitting Yahoo for delisted/odd symbols on every entry check.
_CACHE: Dict[str, Optional[str]] = {}
_LOCK = threading.Lock()

UNKNOWN_SECTOR = "Unknown"


def get_sector(symbol: str) -> Optional[str]:
    """Return the GICS-style sector for *symbol*, or ``None`` if unavailable.

    ``None`` means "could not determine" -- callers must treat it as a
    skipped check, not as a distinct sector.  Symbols that resolve but
    carry no sector (ETFs, some ADRs) return ``UNKNOWN_SECTOR`` so they
    are grouped together rather than silently bypassing the cap.

    Parameters
    ----------
    symbol : str
        Ticker symbol.

    Returns
    -------
    str or None
        Sector name, ``UNKNOWN_SECTOR``, or ``None`` on lookup failure.
    """
    key = symbol.upper()

    with _LOCK:
        if key in _CACHE:
            return _CACHE[key]

    try:
        sector = _fetch_sector(key)
    except Exception as exc:
        # Fail open -- see module docstring.  Not cached: a transient
        # outage should not poison the cache for the rest of the session.
        logger.warning("Sector lookup failed for %s: %s", key, exc)
        return None

    with _LOCK:
        _CACHE[key] = sector
    return sector


def _fetch_sector(symbol: str) -> Optional[str]:
    """Fetch the sector for *symbol* from Yahoo Finance.

    Separated from ``get_sector`` so the cache layer can be tested
    without network access.  Raises on transport failure.
    """
    import yfinance as yf

    from tradingagents.dataflows.stockstats_utils import yf_retry

    info = yf_retry(lambda: yf.Ticker(symbol).info)
    if not info:
        return None
    return info.get("sector") or UNKNOWN_SECTOR


def prime_cache(mapping: Dict[str, Optional[str]]) -> None:
    """Seed the cache from already-known sectors (e.g. stored position rows).

    Avoids a network round-trip per held position when the guardrail sums
    sector exposure.
    """
    with _LOCK:
        for sym, sec in mapping.items():
            if sec:
                _CACHE[sym.upper()] = sec


def clear_cache() -> None:
    """Drop all cached sectors.  Used by tests."""
    with _LOCK:
        _CACHE.clear()
