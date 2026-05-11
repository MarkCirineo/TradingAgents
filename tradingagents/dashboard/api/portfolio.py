"""Portfolio, account, and position API endpoints.

Data source policy:
- **AlpacaClient** is the **source of truth** for all live data:
  portfolio value, cash, positions, bracket order legs.
- **TradeDB** (SQLite) is used only for optional metadata enrichment
  (day count, pipeline mode, entry ORL/LOD, etc.) and historical
  snapshots for daily P&L calculation.
- The Orders page is the only consumer that reads directly from TradeDB.
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
    """Return open positions from Alpaca (source of truth).

    Alpaca's ``get_all_positions()`` determines which positions are
    actually open.  DB metadata (day count, pipeline mode, ORL/LOD)
    is merged in as optional enrichment only.
    """
    from tradingagents.dashboard.app import get_alpaca_client, get_trade_db

    client = get_alpaca_client()
    if not client:
        return {"positions": [], "count": 0, "source": "unavailable"}

    db = get_trade_db()

    # ── Step 1: Alpaca positions (source of truth) ─────────────
    try:
        alpaca_positions = client.get_all_positions()
    except Exception as exc:
        logger.warning("Failed to get Alpaca positions: %s", exc)
        return {"positions": [], "count": 0, "source": "error: broker_unavailable"}

    # ── Step 2: Bracket order legs for stop/TP prices ──────────
    bracket_legs: dict = {}
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

    # ── Step 3: DB metadata for enrichment (optional) ──────────
    db_map: dict = {}
    try:
        for pos in db.get_open_positions():
            db_map[pos["symbol"]] = pos
    except Exception:
        pass  # DB enrichment is best-effort

    # ── Step 4: Build response driven by Alpaca ────────────────
    merged = []
    for pos in alpaca_positions:
        symbol = pos.symbol
        db_data = db_map.get(symbol, {})
        legs = bracket_legs.get(symbol, {})

        merged.append({
            # Alpaca live data (source of truth)
            "symbol": symbol,
            "entry_price": float(pos.avg_entry_price),
            "current_price": round(float(pos.current_price), 2),
            "current_qty": int(float(pos.qty)),
            "market_value": round(float(pos.market_value), 2),
            "unrealized_pl": round(float(pos.unrealized_pl), 2),
            "unrealized_plpc": round(float(pos.unrealized_plpc) * 100, 2),
            "cost_basis": round(float(pos.cost_basis), 2),

            # DB enrichment (optional metadata)
            "entry_date": db_data.get("entry_date"),
            "entry_orl": db_data.get("entry_orl"),
            "entry_lod": db_data.get("entry_lod"),
            "original_qty": db_data.get("original_qty"),
            "day_count": db_data.get("day_count", 1),
            "trimmed": bool(db_data.get("trimmed", 0)),
            "breakeven_stop_active": bool(db_data.get("breakeven_stop_active", 0)),
            "trailing_stop_active": bool(db_data.get("trailing_stop_active", 0)),
            "pipeline_mode": db_data.get("pipeline_mode", "quant"),
            "stop_order_id": db_data.get("stop_order_id"),

            # Bracket legs
            "stop_price": legs.get("stop_price"),
            "take_profit_price": legs.get("take_profit_price"),
        })

    return {"positions": merged, "count": len(merged), "source": "alpaca_live"}


# ---------------------------------------------------------------------------
# /api/positions/{symbol}
# ---------------------------------------------------------------------------

@router.get("/positions/{symbol}")
async def get_position_detail(symbol: str):
    """Return detailed position info for a single symbol.

    Alpaca is the source of truth for whether the position exists.
    DB data provides optional enrichment (entry ORL/LOD, pipeline mode,
    day count, etc.).  Bracket order legs and related DB orders are
    included when available.
    """
    from tradingagents.dashboard.app import get_alpaca_client, get_trade_db

    client = get_alpaca_client()
    db = get_trade_db()
    symbol = symbol.upper()

    # ── Primary: Alpaca live position ──────────────────────────
    live_data = {}
    if client:
        try:
            alpaca_pos = client.get_position(symbol)
            if alpaca_pos:
                live_data = {
                    "current_price": float(alpaca_pos.current_price),
                    "market_value": float(alpaca_pos.market_value),
                    "unrealized_pl": float(alpaca_pos.unrealized_pl),
                    "unrealized_plpc": float(alpaca_pos.unrealized_plpc) * 100,
                    "avg_entry_price": float(alpaca_pos.avg_entry_price),
                    "cost_basis": float(alpaca_pos.cost_basis),
                    "qty": float(alpaca_pos.qty),
                }
        except Exception as exc:
            logger.warning("Failed to get Alpaca position for %s: %s", symbol, exc)

    # ── Enrichment: DB position metadata ───────────────────────
    db_pos = db.get_position(symbol)

    # If neither Alpaca nor DB knows this symbol, 404
    if not live_data and not db_pos:
        raise HTTPException(404, f"No position found for {symbol}")

    # Build a merged position dict (Alpaca wins for price data)
    pos = db_pos or {}
    if live_data:
        pos["current_price"] = live_data["current_price"]
        pos["market_value"] = live_data["market_value"]
        pos["unrealized_pl"] = live_data["unrealized_pl"]
        pos["unrealized_plpc"] = live_data["unrealized_plpc"]
        pos.setdefault("entry_price", live_data["avg_entry_price"])
        pos.setdefault("current_qty", int(live_data.get("qty", 0)))

    # ── Bracket order legs ─────────────────────────────────────
    bracket_info = {}
    stop_order_id = pos.get("stop_order_id") if pos else None
    if client and stop_order_id:
        try:
            order = client.get_order_nested(stop_order_id)
            if order and hasattr(order, "legs") and order.legs:
                bracket_info = {
                    "parent_order": _serialize_alpaca_obj(order),
                    "legs": [_serialize_alpaca_obj(leg) for leg in order.legs],
                }
        except Exception as exc:
            logger.warning("Failed to get bracket info for %s: %s", symbol, exc)

    # ── Related orders from DB ─────────────────────────────────
    related_orders = db.get_orders_for_symbol(symbol)

    return {
        "position": pos,
        "live": live_data,
        "bracket": bracket_info,
        "orders": related_orders,
        "source": "alpaca_live" if live_data else "db_only",
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
