"""Orders API endpoints.

Serves order history from the SQLite database with optional
filtering and bracket order expansion via Alpaca.
"""

from __future__ import annotations

import csv
import io
import logging
from typing import Optional

from fastapi import APIRouter, Query
from fastapi.responses import StreamingResponse

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/orders")
async def get_orders(
    status: Optional[str] = Query(None, description="Filter by status: FILLED, CANCELLED, etc."),
    symbol: Optional[str] = Query(None, description="Filter by symbol"),
    limit: int = Query(200, ge=1, le=1000),
):
    """Return order history with optional filtering."""
    from tradingagents.dashboard.app import get_trade_db

    db = get_trade_db()
    orders = db.get_all_orders(status=status, symbol=symbol, limit=limit)

    # Compute summary stats
    filled = [o for o in orders if o.get("status") == "FILLED"]
    cancelled = [o for o in orders if o.get("status") == "CANCELLED"]
    buys = [o for o in filled if o.get("side") == "BUY"]
    sells = [o for o in filled if o.get("side") == "SELL"]

    return {
        "orders": orders,
        "count": len(orders),
        "summary": {
            "total": len(orders),
            "filled": len(filled),
            "cancelled": len(cancelled),
            "buys": len(buys),
            "sells": len(sells),
        },
    }


@router.get("/orders/{order_id}")
async def get_order_detail(order_id: str):
    """Return a single order with bracket leg expansion."""
    from tradingagents.dashboard.app import get_alpaca_client, get_trade_db

    db = get_trade_db()

    # Get from DB
    with db._connect() as conn:
        row = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
    if not row:
        from fastapi import HTTPException
        raise HTTPException(404, f"Order {order_id} not found")

    order = dict(row)

    # Try to get bracket legs from Alpaca
    bracket_legs = []
    client = get_alpaca_client()
    if client:
        try:
            alpaca_order = client.get_order_nested(order_id)
            if alpaca_order and hasattr(alpaca_order, "legs") and alpaca_order.legs:
                from tradingagents.dashboard.api.portfolio import _serialize_alpaca_obj
                bracket_legs = [_serialize_alpaca_obj(leg) for leg in alpaca_order.legs]
        except Exception as exc:
            logger.warning("Failed to get bracket legs for order %s: %s", order_id, exc)

    return {
        "order": order,
        "bracket_legs": bracket_legs,
    }


@router.get("/orders/export/csv")
async def export_orders_csv(
    status: Optional[str] = Query(None),
    symbol: Optional[str] = Query(None),
):
    """Export orders as CSV download."""
    from tradingagents.dashboard.app import get_trade_db

    db = get_trade_db()
    orders = db.get_all_orders(status=status, symbol=symbol, limit=10000)

    output = io.StringIO()
    if orders:
        writer = csv.DictWriter(output, fieldnames=orders[0].keys())
        writer.writeheader()
        writer.writerows(orders)

    output.seek(0)
    return StreamingResponse(
        output,
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=orders.csv"},
    )
