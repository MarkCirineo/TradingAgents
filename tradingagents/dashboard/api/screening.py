"""Screening log API endpoints.

Serves screening results from the SQLite screening_log table
and today's order activity for the dashboard's activity panel.
"""

from __future__ import annotations

import logging
from datetime import date

from fastapi import APIRouter

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/screening/latest")
async def get_screening_latest():
    """Return today's screening results + activity summary."""
    from tradingagents.dashboard.app import get_trade_db

    db = get_trade_db()
    today = date.today().isoformat()

    screening = db.get_screening_results(today)

    # Compute funnel numbers
    total_screened = len(screening)
    selected = [s for s in screening if s.get("selected_for_pipeline")]
    analyzed = len(selected)
    entries = len([s for s in selected if s.get("signal_result") in ("Buy", "Overweight")])
    rejected = len([s for s in selected if s.get("signal_result") in ("Hold", "Sell", "Underweight")])

    # Get today's orders for the activity feed
    orders = _get_todays_orders(db, today)

    return {
        "date": today,
        "screening": screening,
        "funnel": {
            "screened": total_screened,
            "filtered": total_screened,  # pre-filter count not stored separately
            "analyzed": analyzed,
            "entries": entries,
        },
        "orders": orders,
        "summary": {
            "total_screened": total_screened,
            "sent_to_pipeline": analyzed,
            "entries": entries,
            "rejected": rejected,
        },
    }


@router.get("/screening/{target_date}")
async def get_screening_by_date(target_date: str):
    """Return screening results for a specific date."""
    from tradingagents.dashboard.app import get_trade_db

    db = get_trade_db()
    screening = db.get_screening_results(target_date)

    return {
        "date": target_date,
        "screening": screening,
        "count": len(screening),
    }


def _get_todays_orders(db, today: str) -> list[dict]:
    """Get orders submitted today from the DB."""
    with db._connect() as conn:
        rows = conn.execute(
            "SELECT * FROM orders WHERE submitted_at LIKE ? ORDER BY submitted_at DESC",
            (f"{today}%",),
        ).fetchall()
        return [dict(row) for row in rows]
