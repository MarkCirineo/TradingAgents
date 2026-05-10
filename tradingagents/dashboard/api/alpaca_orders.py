"""Alpaca live orders API endpoint.

Fetches orders directly from the Alpaca API with bracket leg
expansion (nested=True). This is the source of truth for all
order data, including the stop-loss and take-profit child order
prices that Alpaca's own UI doesn't display.
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Query

logger = logging.getLogger(__name__)
router = APIRouter()


def _serialize_order(order) -> dict:
    """Convert an Alpaca Order object to a JSON-serializable dict.

    Handles nested bracket legs, enum values, and datetime objects.
    """
    if order is None:
        return {}

    result = {}
    for attr in [
        "id", "client_order_id", "created_at", "updated_at",
        "submitted_at", "filled_at", "expired_at", "canceled_at",
        "asset_id", "symbol", "asset_class",
        "qty", "filled_qty",
        "order_type", "type", "side",
        "time_in_force",
        "limit_price", "stop_price", "filled_avg_price",
        "status",
        "extended_hours", "trail_percent", "trail_price",
        "hwm",  # high water mark for trailing stops
    ]:
        val = getattr(order, attr, None)
        if val is None:
            result[attr] = None
        elif hasattr(val, "isoformat"):
            result[attr] = val.isoformat()
        elif hasattr(val, "value"):
            # Alpaca enums have a .value attribute
            result[attr] = str(val.value) if hasattr(val, "value") else str(val)
        elif isinstance(val, (int, float, str, bool)):
            result[attr] = val
        else:
            result[attr] = str(val)

    # Parse numeric fields
    for field in ["qty", "filled_qty", "limit_price", "stop_price", "filled_avg_price",
                  "trail_percent", "trail_price", "hwm"]:
        if result.get(field) is not None:
            try:
                result[field] = float(result[field])
            except (ValueError, TypeError):
                pass

    # Bracket legs (the hero feature!)
    legs = getattr(order, "legs", None)
    if legs:
        result["legs"] = [_serialize_order(leg) for leg in legs]
        # Extract stop/TP prices for easy access
        for leg in result["legs"]:
            leg_type = str(leg.get("order_type") or leg.get("type") or "").lower()
            if "stop" in leg_type and leg.get("stop_price"):
                result["stop_loss_price"] = leg["stop_price"]
                result["stop_loss_status"] = leg.get("status")
            elif "limit" in leg_type and leg.get("limit_price"):
                result["take_profit_price"] = leg["limit_price"]
                result["take_profit_status"] = leg.get("status")
    else:
        result["legs"] = []

    return result


@router.get("/alpaca/orders")
async def get_alpaca_orders(
    status: str = Query("all", description="Order status: open, closed, all"),
    symbol: Optional[str] = Query(None, description="Filter by symbol"),
    limit: int = Query(100, ge=1, le=500),
):
    """Fetch orders directly from Alpaca API with bracket leg expansion.

    This is the source of truth — shows stop-loss and take-profit
    prices that Alpaca's own dashboard doesn't display.
    """
    from tradingagents.dashboard.app import get_alpaca_client

    client = get_alpaca_client()
    if not client:
        raise HTTPException(503, "Alpaca client not available")

    try:
        from alpaca.trading.enums import QueryOrderStatus

        # Map status string to enum
        status_map = {
            "open": QueryOrderStatus.OPEN,
            "closed": QueryOrderStatus.CLOSED,
            "all": QueryOrderStatus.ALL,
        }
        query_status = status_map.get(status.lower(), QueryOrderStatus.ALL)

        symbols = [symbol.upper()] if symbol else None
        orders = client.get_orders_nested(status=query_status, symbols=symbols)

        # Serialize and limit
        serialized = [_serialize_order(o) for o in orders][:limit]

        # Summary stats
        filled = [o for o in serialized if str(o.get("status", "")).lower() == "filled"]
        open_orders = [o for o in serialized if str(o.get("status", "")).lower() in ("new", "accepted", "partially_filled")]
        canceled = [o for o in serialized if str(o.get("status", "")).lower() in ("canceled", "cancelled")]
        bracket_orders = [o for o in serialized if len(o.get("legs", [])) > 0]

        return {
            "orders": serialized,
            "count": len(serialized),
            "summary": {
                "total": len(serialized),
                "filled": len(filled),
                "open": len(open_orders),
                "canceled": len(canceled),
                "bracket_orders": len(bracket_orders),
            },
            "source": "alpaca_live",
        }
    except Exception as exc:
        logger.error("Failed to fetch Alpaca orders: %s", exc, exc_info=True)
        raise HTTPException(500, f"Failed to fetch orders: {exc}")
