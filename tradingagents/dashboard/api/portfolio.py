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

def _is_sell_stop(order: dict) -> bool:
    """True when *order* is a protective sell-stop (standalone or leg)."""
    side = str(order.get("side") or "").lower()
    otype = str(order.get("order_type") or order.get("type") or "").lower()
    return "sell" in side and "stop" in otype


def _order_is_open(order: dict) -> bool:
    status = str(order.get("status") or "").lower()
    return status in ("new", "accepted", "held", "pending_new", "partially_filled")


def find_protective_stop(orders: list[dict]) -> Optional[dict]:
    """Find the live protective sell-stop among serialized Alpaca orders.

    The daemon replaces the entry bracket's stop leg with standalone GTC
    stops as it raises the stop, so the live stop may be either a
    top-level order or a still-open bracket leg.  Shared with the
    positions list endpoint (Phase B).
    """
    candidates = []
    for order in orders:
        if _is_sell_stop(order) and _order_is_open(order):
            candidates.append(order)
        for leg in order.get("legs", []):
            if _is_sell_stop(leg) and _order_is_open(leg):
                candidates.append(leg)
    if not candidates:
        return None
    # Most recently submitted wins (latest stop raise)
    candidates.sort(key=lambda o: o.get("submitted_at") or "", reverse=True)
    return candidates[0]


def _build_lifecycle_events(pos: dict, orders: list[dict]) -> list[dict]:
    """Derive a lifecycle timeline from the DB row + Alpaca order history."""
    timed: list[dict] = []
    untimed: list[dict] = []

    def add(ts, label, detail, variant):
        item = {"ts": ts or "", "label": label, "detail": detail, "variant": variant}
        (timed if ts else untimed).append(item)

    def _money(val):
        try:
            return f"${float(val):,.2f}"
        except (TypeError, ValueError):
            return "$—"

    # -- entry order ----------------------------------------------------
    entry_id = pos.get("entry_order_id") or pos.get("stop_order_id")
    entry_order = next((o for o in orders if o.get("id") == entry_id), None)
    if entry_order is None:
        buys = [
            o for o in orders
            if "buy" in str(o.get("side") or "").lower() and not o.get("parent_id")
        ]
        buys.sort(key=lambda o: o.get("submitted_at") or "")
        entry_order = buys[0] if buys else None

    if entry_order:
        otype = str(entry_order.get("order_type") or entry_order.get("type") or "?")
        ref_price = entry_order.get("stop_price") or entry_order.get("limit_price")
        detail = f"{otype} order, {int(float(entry_order.get('qty') or 0))} shares"
        if ref_price:
            detail += f" @ {_money(ref_price)} trigger"
        add(entry_order.get("submitted_at"), "Entry submitted", detail, "info")

        if entry_order.get("filled_at"):
            add(
                entry_order.get("filled_at"),
                "Entry filled",
                f"{int(float(entry_order.get('filled_qty') or 0))} shares "
                f"@ {_money(entry_order.get('filled_avg_price'))}",
                "primary",
            )
        elif str(entry_order.get("status") or "").lower() in ("canceled", "cancelled", "expired"):
            add(
                entry_order.get("canceled_at") or entry_order.get("expired_at"),
                "Entry cancelled",
                "Pivot never triggered",
                "neutral",
            )
    elif pos.get("entry_date"):
        add(
            pos.get("entry_date"),
            "Entry submitted" if pos.get("status") == "PENDING" else "Opened",
            f"{int(pos.get('original_qty') or pos.get('current_qty') or 0)} shares "
            f"@ {_money(pos.get('entry_price'))}",
            "info" if pos.get("status") == "PENDING" else "primary",
        )

    # -- initial stop ---------------------------------------------------
    if pos.get("entry_orl"):
        add(
            (entry_order or {}).get("filled_at") or pos.get("entry_date"),
            "Initial stop set",
            f"{_money(pos.get('entry_orl'))} (consolidation pivot floor)",
            "danger",
        )

    # -- subsequent sell orders: stop raises, trims, exits --------------
    for order in orders:
        if entry_order and order.get("id") == entry_order.get("id"):
            continue
        side = str(order.get("side") or "").lower()
        if "sell" not in side:
            continue
        otype = str(order.get("order_type") or order.get("type") or "").lower()
        status = str(order.get("status") or "").lower()

        if "stop" in otype:
            if status == "filled":
                add(
                    order.get("filled_at"), "Stopped out",
                    f"{int(float(order.get('filled_qty') or 0))} shares "
                    f"@ {_money(order.get('filled_avg_price'))}",
                    "danger",
                )
            elif _order_is_open(order):
                add(
                    order.get("submitted_at"), "Stop raised",
                    f"{_money(order.get('stop_price'))} (current, GTC)",
                    "warning",
                )
            else:
                add(
                    order.get("submitted_at"), "Stop raised",
                    f"{_money(order.get('stop_price'))} (later superseded)",
                    "neutral",
                )
        elif status == "filled":
            add(
                order.get("filled_at"), "Sold",
                f"{int(float(order.get('filled_qty') or 0))} shares "
                f"@ {_money(order.get('filled_avg_price'))}",
                "warning",
            )

    # -- DB state flags -------------------------------------------------
    if pos.get("trimmed"):
        add(
            pos.get("trim_date"), "Trimmed",
            f"{int(pos.get('current_qty') or 0)} shares remaining", "warning",
        )
    if pos.get("breakeven_stop_active"):
        untimed.append({
            "ts": "", "label": "Breakeven stop active",
            "detail": f"Stop at entry: {_money(pos.get('entry_price'))}",
            "variant": "info",
        })
    if pos.get("trailing_stop_active"):
        untimed.append({
            "ts": "", "label": "Trailing stop active",
            "detail": "Tracking the 10-day SMA", "variant": "success",
        })
    if pos.get("status") == "CLOSED":
        add(
            pos.get("closed_at"), "Closed",
            pos.get("close_reason") or "Manual", "neutral",
        )
    elif pos.get("status") == "CANCELLED":
        add(
            pos.get("closed_at"), "Entry cancelled",
            pos.get("close_reason") or "Never filled", "neutral",
        )

    timed.sort(key=lambda e: e["ts"])
    return timed + untimed


@router.get("/positions/{symbol}")
async def get_position_detail(symbol: str):
    """Return detailed position info for a single symbol.

    Alpaca is the source of truth: live position data, the actual open
    protective stop order (standalone GTC or bracket leg), and the full
    order history for the symbol.  The DB row supplies strategy metadata
    (initial stop, day count, pipeline mode, trim state) and covers
    PENDING entries that have no Alpaca position yet.
    """
    from tradingagents.dashboard.api.alpaca_orders import _serialize_order
    from tradingagents.dashboard.app import get_alpaca_client, get_trade_db

    client = get_alpaca_client()
    db = get_trade_db()
    symbol = symbol.upper()

    # ── Live Alpaca position ───────────────────────────────────
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

    # ── DB position metadata ───────────────────────────────────
    db_pos = db.get_position(symbol)

    # If neither Alpaca nor DB knows this symbol, 404
    if not live_data and not db_pos:
        raise HTTPException(404, f"No position found for {symbol}")

    # Merged view: Alpaca wins for anything it knows (the DB entry
    # price is the intended signal price until fill reconciliation
    # runs; Alpaca's avg_entry_price is the actual fill).
    pos = dict(db_pos) if db_pos else {}
    if live_data:
        pos["current_price"] = live_data["current_price"]
        pos["market_value"] = live_data["market_value"]
        pos["unrealized_pl"] = live_data["unrealized_pl"]
        pos["unrealized_plpc"] = live_data["unrealized_plpc"]
        pos["entry_price"] = live_data["avg_entry_price"]
        pos.setdefault("current_qty", int(live_data.get("qty", 0)))
        pos.setdefault("status", "OPEN")

    # ── Full Alpaca order history for this symbol ──────────────
    alpaca_orders: list[dict] = []
    if client:
        try:
            from alpaca.trading.enums import QueryOrderStatus
            raw = client.get_orders_nested(
                status=QueryOrderStatus.ALL, symbols=[symbol]
            )
            alpaca_orders = [_serialize_order(o) for o in raw]
        except Exception as exc:
            logger.warning("Failed to get Alpaca orders for %s: %s", symbol, exc)

    # ── Protection: initial stop vs live stop ──────────────────
    live_stop = find_protective_stop(alpaca_orders)
    initial_stop = pos.get("entry_orl")
    current_stop = (
        live_stop.get("stop_price") if live_stop
        else pos.get("current_stop") or initial_stop
    )
    entry_price = pos.get("entry_price") or 0
    risk_per_share = (
        round(entry_price - initial_stop, 4)
        if entry_price and initial_stop else None
    )
    r_multiple = None
    if risk_per_share and risk_per_share > 0 and live_data:
        r_multiple = round(
            (live_data["current_price"] - entry_price) / risk_per_share, 2
        )

    protection = {
        "initial_stop": initial_stop,
        "current_stop": current_stop,
        "stop_order": live_stop,          # None when no live stop order found
        "stop_source": "alpaca_order" if live_stop else "db",
        "risk_per_share": risk_per_share,
        "r_multiple": r_multiple,
    }

    return {
        "position": pos,
        "live": live_data,
        "protection": protection,
        "orders": alpaca_orders,
        "events": _build_lifecycle_events(pos, alpaca_orders),
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
