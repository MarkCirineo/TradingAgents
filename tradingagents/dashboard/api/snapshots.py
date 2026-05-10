"""Snapshot and equity curve API endpoints.

Serves historical portfolio data from the SQLite daily_snapshots
table for charting and analysis.
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/snapshots")
async def get_snapshots(days: int = 30):
    """Return recent daily portfolio snapshots.

    Used for the equity curve chart and historical analysis.
    """
    from tradingagents.dashboard.app import get_trade_db

    db = get_trade_db()
    snapshots = db.get_recent_snapshots(days=days)

    # Reverse to chronological order (DB returns most recent first)
    snapshots.reverse()

    return {"snapshots": snapshots, "count": len(snapshots)}


@router.get("/equity-curve")
async def get_equity_curve(days: int = 90):
    """Return portfolio value time series formatted for charting.

    Returns data in TradingView Lightweight Charts format:
    [{time: "YYYY-MM-DD", value: 100000}, ...]
    """
    from tradingagents.dashboard.app import get_trade_db

    db = get_trade_db()
    snapshots = db.get_recent_snapshots(days=days)

    # Reverse to chronological order and format for charts
    snapshots.reverse()
    chart_data = [
        {"time": s["date"], "value": s["portfolio_value"]}
        for s in snapshots
        if s.get("portfolio_value") is not None
    ]

    return {"data": chart_data, "count": len(chart_data)}
