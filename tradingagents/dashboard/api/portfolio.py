"""Portfolio, account, and position API endpoints.

These endpoints merge data from two sources:
- **TradeDB** (SQLite): Our tracked positions, historical data
- **AlpacaClient**: Live account/portfolio data, bracket order legs
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Any, Optional

from fastapi import APIRouter, HTTPException

logger = logging.getLogger(__name__)
router = APIRouter()


def _serialize_alpaca_obj(obj) -> dict:
    """Convert an Alpaca API object to a JSON-serializable dict."""
    if obj is None:
        return {}
    if hasattr(obj, "__dict__"):
        result = {}
        for k, v in obj.__dict__.items():
            if k.startswith("_"):
                continue
            if hasattr(v, "isoformat"):
                result[k] = v.isoformat()
            elif hasattr(v, "__dict__"):
                result[k] = _serialize_alpaca_obj(v)
            elif isinstance(v, list):
                result[k] = [_serialize_alpaca_obj(item) if hasattr(item, "__dict__") else item for item in v]
            elif isinstance(v, (int, float, str, bool, type(None))):
                result[k] = v
            else:
                result[k] = str(v)
        return result
    return str(obj)


# ---------------------------------------------------------------------------
# /api/portfolio
# ---------------------------------------------------------------------------

@router.get("/portfolio")
async def get_portfolio():
    """Return current portfolio summary.

    Merges Alpaca account data with TradeDB position count.
    """
    from tradingagents.dashboard.app import get_alpaca_client, get_trade_db

    client = get_alpaca_client()
    db = get_trade_db()

    result = {
        "portfolio_value": 0,
        "cash": 0,
        "buying_power": 0,
        "equity": 0,
        "exposure_pct": 0,
        "daily_pnl": 0,
        "daily_pnl_pct": 0,
        "positions_count": 0,
        "market_open": False,
        "source": "unavailable",
    }

    # Live data from Alpaca
    if client:
        try:
            account = client.get_account()
            portfolio_value = float(account.portfolio_value)
            cash = float(account.cash)
            equity = float(account.equity)
            buying_power = float(account.buying_power)
            invested = portfolio_value - cash

            # Get today's snapshot for P&L comparison
            today = date.today().isoformat()
            snapshot = db.get_daily_snapshot(today)
            daily_pnl = 0
            daily_pnl_pct = 0
            if snapshot and snapshot.get("portfolio_value"):
                # Compare current value to start-of-day snapshot
                pass  # TODO: compute from previous day's snapshot

            # Get previous day's snapshot for daily P&L
            snapshots = db.get_recent_snapshots(days=2)
            if len(snapshots) >= 1:
                prev_value = snapshots[0].get("portfolio_value", portfolio_value)
                daily_pnl = portfolio_value - prev_value
                daily_pnl_pct = (daily_pnl / prev_value * 100) if prev_value > 0 else 0

            positions = client.get_all_positions()

            result.update({
                "portfolio_value": round(portfolio_value, 2),
                "cash": round(cash, 2),
                "buying_power": round(buying_power, 2),
                "equity": round(equity, 2),
                "exposure_pct": round((invested / portfolio_value * 100) if portfolio_value > 0 else 0, 1),
                "daily_pnl": round(daily_pnl, 2),
                "daily_pnl_pct": round(daily_pnl_pct, 2),
                "positions_count": len(positions),
                "market_open": client.is_market_open(),
                "source": "alpaca_live",
            })
        except Exception as exc:
            logger.warning("Failed to get portfolio from Alpaca: %s", exc)
            result["source"] = "error: broker_unavailable"

    return result


# ---------------------------------------------------------------------------
# /api/account
# ---------------------------------------------------------------------------

@router.get("/account")
async def get_account():
    """Return full Alpaca account details."""
    from tradingagents.dashboard.app import get_alpaca_client

    client = get_alpaca_client()
    if not client:
        raise HTTPException(503, "Alpaca client not available")

    try:
        account = client.get_account()
        return _serialize_alpaca_obj(account)
    except Exception as exc:
        logger.error("Failed to get account: %s", exc, exc_info=True)
        raise HTTPException(500, "Failed to get account")


# ---------------------------------------------------------------------------
# /api/positions
# ---------------------------------------------------------------------------

@router.get("/positions")
async def get_positions():
    """Return open positions with live Alpaca data + bracket leg prices.

    Merges:
    - TradeDB position records (entry price, day count, stop levels)
    - Alpaca live position data (current price, unrealized P&L)
    - Alpaca bracket order legs (stop-loss price, take-profit price)
    """
    from tradingagents.dashboard.app import get_alpaca_client, get_trade_db

    client = get_alpaca_client()
    db = get_trade_db()

    # Get our tracked positions from DB
    db_positions = db.get_open_positions()

    # Get live data from Alpaca
    alpaca_positions = {}
    if client:
        try:
            for pos in client.get_all_positions():
                alpaca_positions[pos.symbol] = {
                    "current_price": float(pos.current_price),
                    "market_value": float(pos.market_value),
                    "unrealized_pl": float(pos.unrealized_pl),
                    "unrealized_plpc": float(pos.unrealized_plpc) * 100,
                    "qty": float(pos.qty),
                    "avg_entry_price": float(pos.avg_entry_price),
                    "cost_basis": float(pos.cost_basis),
                }
        except Exception as exc:
            logger.warning("Failed to get Alpaca positions: %s", exc)

    # Get bracket order legs for stop/TP prices
    bracket_legs = {}
    if client:
        try:
            from alpaca.trading.enums import QueryOrderStatus
            orders = client.get_orders_nested(status=QueryOrderStatus.OPEN)
            for order in orders:
                symbol = order.symbol
                if hasattr(order, "legs") and order.legs:
                    legs_data = {"stop_price": None, "take_profit_price": None}
                    for leg in order.legs:
                        leg_type = str(getattr(leg, "order_type", "")).lower()
                        if "stop" in leg_type:
                            legs_data["stop_price"] = float(leg.stop_price) if leg.stop_price else None
                        elif "limit" in leg_type:
                            legs_data["take_profit_price"] = float(leg.limit_price) if leg.limit_price else None
                    bracket_legs[symbol] = legs_data
        except Exception as exc:
            logger.warning("Failed to get bracket legs: %s", exc)

    # Merge everything
    merged = []
    for pos in db_positions:
        symbol = pos["symbol"]
        live = alpaca_positions.get(symbol, {})
        legs = bracket_legs.get(symbol, {})

        current_price = live.get("current_price", pos.get("entry_price", 0))
        entry_price = pos.get("entry_price", 0)
        qty = pos.get("current_qty", 0)
        unrealized_pl = live.get("unrealized_pl", (current_price - entry_price) * qty if entry_price else 0)
        unrealized_plpc = live.get("unrealized_plpc", 
            ((current_price - entry_price) / entry_price * 100) if entry_price > 0 else 0)

        merged.append({
            # DB data
            "symbol": symbol,
            "entry_date": pos.get("entry_date"),
            "entry_price": entry_price,
            "entry_orl": pos.get("entry_orl"),
            "entry_lod": pos.get("entry_lod"),
            "original_qty": pos.get("original_qty"),
            "current_qty": qty,
            "day_count": pos.get("day_count", 1),
            "trimmed": bool(pos.get("trimmed", 0)),
            "breakeven_stop_active": bool(pos.get("breakeven_stop_active", 0)),
            "trailing_stop_active": bool(pos.get("trailing_stop_active", 0)),
            "pipeline_mode": pos.get("pipeline_mode", "full"),
            "stop_order_id": pos.get("stop_order_id"),

            # Live Alpaca data
            "current_price": round(current_price, 2),
            "market_value": round(live.get("market_value", current_price * qty), 2),
            "unrealized_pl": round(unrealized_pl, 2),
            "unrealized_plpc": round(unrealized_plpc, 2),

            # Bracket legs (hero feature)
            "stop_price": legs.get("stop_price"),
            "take_profit_price": legs.get("take_profit_price"),
        })

    return {"positions": merged, "count": len(merged)}


# ---------------------------------------------------------------------------
# /api/positions/{symbol}
# ---------------------------------------------------------------------------

@router.get("/positions/{symbol}")
async def get_position_detail(symbol: str):
    """Return detailed position info for a single symbol.

    Includes bracket order legs, related orders, and LLM analysis.
    """
    from tradingagents.dashboard.app import get_alpaca_client, get_trade_db

    client = get_alpaca_client()
    db = get_trade_db()

    # DB position
    pos = db.get_position(symbol.upper())
    if not pos:
        raise HTTPException(404, f"No position found for {symbol}")

    # Live Alpaca data
    live_data = {}
    if client:
        try:
            alpaca_pos = client.get_position(symbol.upper())
            if alpaca_pos:
                live_data = {
                    "current_price": float(alpaca_pos.current_price),
                    "market_value": float(alpaca_pos.market_value),
                    "unrealized_pl": float(alpaca_pos.unrealized_pl),
                    "unrealized_plpc": float(alpaca_pos.unrealized_plpc) * 100,
                    "avg_entry_price": float(alpaca_pos.avg_entry_price),
                    "cost_basis": float(alpaca_pos.cost_basis),
                }
        except Exception as exc:
            logger.warning("Failed to get Alpaca position for %s: %s", symbol, exc)

    # Bracket order legs
    bracket_info = {}
    if client and pos.get("stop_order_id"):
        try:
            order = client.get_order_nested(pos["stop_order_id"])
            if order and hasattr(order, "legs") and order.legs:
                bracket_info = {
                    "parent_order": _serialize_alpaca_obj(order),
                    "legs": [_serialize_alpaca_obj(leg) for leg in order.legs],
                }
        except Exception as exc:
            logger.warning("Failed to get bracket info for %s: %s", symbol, exc)

    # Related orders from DB
    related_orders = db.get_orders_for_symbol(symbol.upper())

    return {
        "position": pos,
        "live": live_data,
        "bracket": bracket_info,
        "orders": related_orders,
    }


# ---------------------------------------------------------------------------
# /api/clock
# ---------------------------------------------------------------------------

@router.get("/clock")
async def get_market_clock():
    """Return market clock information."""
    from tradingagents.dashboard.app import get_alpaca_client

    client = get_alpaca_client()
    if not client:
        return {"is_open": False, "source": "unavailable"}

    try:
        clock = client.get_clock()
        return {
            "is_open": clock.is_open,
            "next_open": clock.next_open.isoformat() if clock.next_open else None,
            "next_close": clock.next_close.isoformat() if clock.next_close else None,
            "timestamp": clock.timestamp.isoformat() if clock.timestamp else None,
            "source": "alpaca_live",
        }
    except Exception as exc:
        logger.warning("Failed to get market clock: %s", exc)
        return {"is_open": False, "source": "error: clock_unavailable"}
