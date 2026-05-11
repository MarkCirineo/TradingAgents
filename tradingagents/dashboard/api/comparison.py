"""A/B Comparison API endpoint.

Fetches portfolio and performance data from both the local instance
and a peer instance (configured via PEER_DASHBOARD_URL env var)
to enable side-by-side comparison of execution modes.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Optional

import requests
from fastapi import APIRouter, HTTPException

logger = logging.getLogger(__name__)
router = APIRouter()

PEER_URL = os.getenv("PEER_DASHBOARD_URL", "")


def _fetch_peer(endpoint: str, timeout: float = 5.0) -> Optional[dict]:
    """Fetch data from the peer dashboard instance."""
    if not PEER_URL:
        return None
    try:
        url = f"{PEER_URL.rstrip('/')}/api{endpoint}"
        resp = requests.get(url, timeout=timeout)
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:
        logger.warning("Failed to fetch from peer %s: %s", endpoint, exc)
        return None


@router.get("/comparison")
async def get_comparison():
    """Return side-by-side data from local and peer instances.

    Local data comes from this instance's own API handlers.
    Peer data is fetched via HTTP from the PEER_DASHBOARD_URL.
    """
    from tradingagents.dashboard.app import get_alpaca_client, get_trade_db

    client = get_alpaca_client()
    db = get_trade_db()

    # ── Local instance data ────────────────────────────────────
    local_data = {
        "label": os.getenv("INSTANCE_LABEL", "Instance A"),
        "mode": os.getenv("PIPELINE_MODE", "full"),
        "available": True,
    }

    # Portfolio
    if client:
        try:
            account = client.get_account()
            portfolio_value = float(account.portfolio_value)
            cash = float(account.cash)
            equity = float(account.equity)
            last_equity = float(account.last_equity) if hasattr(account, "last_equity") else equity

            local_data["portfolio"] = {
                "portfolio_value": round(portfolio_value, 2),
                "cash": round(cash, 2),
                "equity": round(equity, 2),
                "daily_pnl": round(equity - last_equity, 2),
                "daily_pnl_pct": round(((equity - last_equity) / last_equity * 100) if last_equity > 0 else 0, 2),
            }
        except Exception as exc:
            logger.warning("Failed to get local portfolio: %s", exc)
            local_data["portfolio"] = {}
    else:
        local_data["portfolio"] = {}

    # Positions (Alpaca = source of truth for open positions)
    open_positions = []
    if client:
        try:
            open_positions = client.get_all_positions()
        except Exception as exc:
            logger.warning("Failed to get Alpaca positions for comparison: %s", exc)
    closed = db.get_closed_positions(limit=50)
    local_data["positions"] = {
        "open": len(open_positions),
        "closed": len(closed),
        "symbols": [p.symbol for p in open_positions],
    }

    # Orders
    orders = db.get_all_orders(limit=500)
    filled = [o for o in orders if o.get("status") == "FILLED"]
    local_data["orders"] = {
        "total": len(orders),
        "filled": len(filled),
        "buys": len([o for o in filled if o.get("side") == "BUY"]),
        "sells": len([o for o in filled if o.get("side") == "SELL"]),
    }

    # Equity curve
    snapshots = db.get_recent_snapshots(days=30)
    local_data["equity_curve"] = [
        {"date": s["date"], "value": s.get("portfolio_value", 0)}
        for s in snapshots
    ]

    # ── Peer instance data ─────────────────────────────────────
    peer_data = {"label": "Peer Instance", "mode": "unknown", "available": False}

    if PEER_URL:
        # Fetch all peer endpoints concurrently without blocking the event loop
        peer_portfolio, peer_positions, peer_orders, peer_snapshots = await asyncio.gather(
            asyncio.to_thread(_fetch_peer, "/portfolio"),
            asyncio.to_thread(_fetch_peer, "/positions"),
            asyncio.to_thread(_fetch_peer, "/orders"),
            asyncio.to_thread(_fetch_peer, "/snapshots?days=30"),
        )

        if peer_portfolio:
            peer_data["available"] = True
            peer_data["label"] = os.getenv("PEER_LABEL", "Instance B")
            peer_data["mode"] = peer_portfolio.get("pipeline_mode", "unknown")
            peer_data["portfolio"] = {
                "portfolio_value": peer_portfolio.get("portfolio_value", 0),
                "cash": peer_portfolio.get("cash", 0),
                "equity": peer_portfolio.get("equity", 0),
                "daily_pnl": peer_portfolio.get("daily_pnl", 0),
                "daily_pnl_pct": peer_portfolio.get("daily_pnl_pct", 0),
            }

        if peer_positions:
            positions_list = peer_positions.get("positions", [])
            peer_data["positions"] = {
                "open": peer_positions.get("count", 0),
                "closed": 0,
                "symbols": [p.get("symbol") for p in positions_list],
            }

        if peer_orders:
            peer_data["orders"] = peer_orders.get("summary", {})

        if peer_snapshots:
            peer_data["equity_curve"] = [
                {"date": s.get("date"), "value": s.get("portfolio_value", 0)}
                for s in peer_snapshots.get("snapshots", [])
            ]

    return {
        "local": local_data,
        "peer": peer_data,
        "peer_configured": bool(PEER_URL),
    }
