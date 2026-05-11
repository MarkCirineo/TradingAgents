"""Server-Sent Events (SSE) endpoint for real-time dashboard updates.

Pushes portfolio, position, and daemon status updates to connected
browser clients every 30 seconds.  The browser's built-in EventSource
API handles auto-reconnection natively.

Events:
    portfolio_update  — portfolio value, cash, P&L
    positions_update  — open positions with live prices + bracket legs
    daemon_status     — running/idle, last run, pipeline mode
    clock_update      — market open/close status
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import date, datetime

from fastapi import APIRouter
from fastapi.responses import StreamingResponse

logger = logging.getLogger(__name__)
router = APIRouter()

# Update interval in seconds
_SSE_INTERVAL = 30


def _make_sse_event(event_type: str, data: dict) -> str:
    """Format a Server-Sent Event message."""
    payload = json.dumps(data, default=str)
    return f"event: {event_type}\ndata: {payload}\n\n"


async def _event_generator():
    """Async generator that yields SSE events periodically."""
    from tradingagents.dashboard.app import get_alpaca_client, get_trade_db, get_config

    while True:
        try:
            client = get_alpaca_client()
            db = get_trade_db()
            config = get_config()

            # ── Portfolio update ───────────────────────────────────
            portfolio_data = {
                "portfolio_value": 0,
                "cash": 0,
                "daily_pnl": 0,
                "daily_pnl_pct": 0,
                "exposure_pct": 0,
                "market_open": False,
                "timestamp": datetime.now().isoformat(),
            }

            if client:
                try:
                    account = client.get_account()
                    pv = float(account.portfolio_value)
                    cash = float(account.cash)
                    invested = pv - cash

                    # Daily P&L from previous snapshot
                    snapshots = db.get_recent_snapshots(days=2)
                    daily_pnl = 0
                    daily_pnl_pct = 0
                    if snapshots:
                        prev_value = snapshots[0].get("portfolio_value", pv)
                        daily_pnl = pv - prev_value
                        daily_pnl_pct = (daily_pnl / prev_value * 100) if prev_value > 0 else 0

                    portfolio_data.update({
                        "portfolio_value": round(pv, 2),
                        "cash": round(cash, 2),
                        "daily_pnl": round(daily_pnl, 2),
                        "daily_pnl_pct": round(daily_pnl_pct, 2),
                        "exposure_pct": round((invested / pv * 100) if pv > 0 else 0, 1),
                        "market_open": client.is_market_open(),
                    })
                except Exception as exc:
                    logger.debug("SSE portfolio fetch failed: %s", exc)

            yield _make_sse_event("portfolio_update", portfolio_data)

            # ── Positions update (Alpaca = source of truth) ─────────
            positions_data = []
            if client:
                try:
                    alpaca_positions = client.get_all_positions()

                    # Optional DB enrichment
                    db_map = {}
                    try:
                        for p in db.get_open_positions():
                            db_map[p["symbol"]] = p
                    except Exception:
                        pass

                    for pos in alpaca_positions:
                        symbol = pos.symbol
                        db_data = db_map.get(symbol, {})
                        positions_data.append({
                            "symbol": symbol,
                            "entry_price": float(pos.avg_entry_price),
                            "current_price": round(float(pos.current_price), 2),
                            "unrealized_pl": round(float(pos.unrealized_pl), 2),
                            "unrealized_plpc": round(float(pos.unrealized_plpc) * 100, 2),
                            "current_qty": int(float(pos.qty)),
                            "day_count": db_data.get("day_count", 1),
                            "trimmed": bool(db_data.get("trimmed", 0)),
                            "pipeline_mode": db_data.get("pipeline_mode", "quant"),
                        })
                except Exception:
                    pass

            yield _make_sse_event("positions_update", {
                "positions": positions_data,
                "count": len(positions_data),
            })

            # ── Daemon status ──────────────────────────────────────
            daemon_data = {
                "pipeline_mode": config.get("pipeline_mode", "full"),
                "timestamp": datetime.now().isoformat(),
            }
            yield _make_sse_event("daemon_status", daemon_data)

            # ── Clock update ───────────────────────────────────────
            if client:
                try:
                    clock = client.get_clock()
                    yield _make_sse_event("clock_update", {
                        "is_open": clock.is_open,
                        "next_open": clock.next_open.isoformat() if clock.next_open else None,
                        "next_close": clock.next_close.isoformat() if clock.next_close else None,
                    })
                except Exception:
                    pass

        except Exception as exc:
            logger.error("SSE event generation error: %s", exc)
            yield _make_sse_event("error", {"message": str(exc)})

        # Wait before next batch of events
        await asyncio.sleep(_SSE_INTERVAL)


@router.get("/stream")
async def sse_stream():
    """Server-Sent Events endpoint for real-time dashboard updates.

    Connect from the browser with:
        const es = new EventSource('/api/stream');
        es.addEventListener('portfolio_update', (e) => { ... });
    """
    return StreamingResponse(
        _event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # Disable NGINX buffering for SSE
        },
    )
