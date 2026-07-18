"""Market regime API endpoint.

Runs the same ``check_market_regime()`` the daemon uses at pre-market
(SPY vs 10/20 MA stacking + VIX regime adjustments) so the dashboard
shows the bot's actual go/no-go signal instead of a dead placeholder.

Results are cached for 10 minutes — the inputs are daily bars and the
VIX level, so anything fresher is noise.  The endpoint is sync (``def``)
so FastAPI runs it in the threadpool; a cold cache miss makes ~4
blocking market-data calls.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

from fastapi import APIRouter, Query

logger = logging.getLogger(__name__)
router = APIRouter()

_CACHE: dict = {"ts": 0.0, "data": None}
_TTL_SECONDS = 600


@router.get("/regime")
def get_regime(refresh: bool = Query(False, description="Bypass the cache")):
    """Return the current market regime (favorable flag, SPY MAs, VIX)."""
    now = time.time()
    if (
        _CACHE["data"] is not None
        and not refresh
        and now - _CACHE["ts"] < _TTL_SECONDS
    ):
        return _CACHE["data"]

    from tradingagents.screening.pre_filter import check_market_regime

    try:
        result = check_market_regime()
    except Exception as exc:
        logger.error("Regime check failed: %s", exc)
        # Serve stale data over nothing
        if _CACHE["data"] is not None:
            return {**_CACHE["data"], "stale": True}
        return {"favorable": None, "checks": {}, "regime": {},
                "source": f"error: {exc}"}

    data = {
        "favorable": result.get("favorable"),
        "checks": result.get("checks", {}),
        "regime": result.get("regime", {}),
        "as_of": datetime.now(timezone.utc).isoformat(),
        "source": "live",
    }
    _CACHE["ts"] = now
    _CACHE["data"] = data
    return data
