"""Tests for position-detail API helpers in dashboard/api/portfolio.py.

Covers the pure logic added in the Phase C overhaul:
  - find_protective_stop: locating the live sell-stop among Alpaca
    orders (standalone GTC replacement or original bracket leg)
  - _build_lifecycle_events: deriving the position timeline from the
    DB row plus real Alpaca order history
"""

import pytest

from tradingagents.dashboard.api.portfolio import (
    _build_lifecycle_events,
    find_protective_stop,
)


# ---------------------------------------------------------------------------
# Order fixtures (shape matches alpaca_orders._serialize_order output)
# ---------------------------------------------------------------------------

def _order(
    oid="o1", side="buy", order_type="stop", status="filled",
    submitted_at="2026-07-17T13:30:00Z", qty=35, filled_qty=0,
    stop_price=None, limit_price=None, filled_avg_price=None,
    filled_at=None, legs=None, tif="gtc",
):
    return {
        "id": oid,
        "side": f"OrderSide.{side.upper()}" if side else None,
        "order_type": order_type,
        "type": order_type,
        "status": status,
        "submitted_at": submitted_at,
        "filled_at": filled_at,
        "qty": qty,
        "filled_qty": filled_qty,
        "stop_price": stop_price,
        "limit_price": limit_price,
        "filled_avg_price": filled_avg_price,
        "time_in_force": tif,
        "legs": legs or [],
    }


def _achc_history():
    """Recreate the real ACHC order history from the Jul 17 session:
    filled bracket entry (legs cancelled), plus a live GTC stop
    replacement at the raised LOD price.
    """
    entry = _order(
        oid="entry", side="buy", order_type="market", status="filled",
        submitted_at="2026-07-17T13:30:01Z", filled_at="2026-07-17T13:30:02Z",
        qty=35, filled_qty=35, filled_avg_price=33.27,
        legs=[
            _order(oid="leg-tp", side="sell", order_type="limit",
                   status="canceled", limit_price=63.11),
            _order(oid="leg-sl", side="sell", order_type="stop",
                   status="canceled", stop_price=23.79),
        ],
    )
    new_stop = _order(
        oid="stop2", side="sell", order_type="stop", status="new",
        submitted_at="2026-07-17T19:45:03Z", qty=35, stop_price=32.71,
    )
    return [new_stop, entry]  # Alpaca returns most recent first


# ---------------------------------------------------------------------------
# find_protective_stop
# ---------------------------------------------------------------------------

class TestFindProtectiveStop:
    def test_finds_standalone_gtc_replacement_stop(self):
        stop = find_protective_stop(_achc_history())
        assert stop is not None
        assert stop["id"] == "stop2"
        assert stop["stop_price"] == 32.71

    def test_finds_open_bracket_leg_when_no_replacement(self):
        entry = _order(
            oid="entry", side="buy", order_type="market", status="filled",
            legs=[
                _order(oid="leg-sl", side="sell", order_type="stop",
                       status="new", stop_price=23.79),
            ],
        )
        stop = find_protective_stop([entry])
        assert stop["id"] == "leg-sl"
        assert stop["stop_price"] == 23.79

    def test_ignores_cancelled_stops_and_buy_stops(self):
        orders = [
            _order(oid="old-stop", side="sell", order_type="stop",
                   status="canceled", stop_price=30.00),
            _order(oid="entry-stop", side="buy", order_type="stop",
                   status="new", stop_price=7.22),
        ]
        assert find_protective_stop(orders) is None

    def test_most_recent_open_stop_wins(self):
        orders = [
            _order(oid="s1", side="sell", order_type="stop", status="new",
                   submitted_at="2026-07-16T19:45:00Z", stop_price=30.00),
            _order(oid="s2", side="sell", order_type="stop", status="new",
                   submitted_at="2026-07-17T19:45:00Z", stop_price=32.71),
        ]
        assert find_protective_stop(orders)["id"] == "s2"

    def test_empty_orders(self):
        assert find_protective_stop([]) is None


# ---------------------------------------------------------------------------
# _build_lifecycle_events
# ---------------------------------------------------------------------------

def _achc_pos(**overrides):
    pos = {
        "symbol": "ACHC",
        "status": "OPEN",
        "entry_date": "2026-07-17",
        "entry_price": 33.27,
        "entry_orl": 23.79,
        "current_stop": 32.71,
        "current_qty": 35,
        "original_qty": 35,
        "entry_order_id": "entry",
        "stop_order_id": "stop2",
        "trimmed": 0,
        "breakeven_stop_active": 0,
        "trailing_stop_active": 0,
    }
    pos.update(overrides)
    return pos


class TestLifecycleEvents:
    def test_full_achc_lifecycle(self):
        events = _build_lifecycle_events(_achc_pos(), _achc_history())
        labels = [e["label"] for e in events]

        assert labels == [
            "Entry submitted",
            "Entry filled",
            "Initial stop set",
            "Stop raised",
        ]
        # Entry fill shows the ACTUAL fill price
        filled = next(e for e in events if e["label"] == "Entry filled")
        assert "$33.27" in filled["detail"]
        # Initial stop shows the pivot floor, not the current stop
        initial = next(e for e in events if e["label"] == "Initial stop set")
        assert "$23.79" in initial["detail"]
        assert "pivot floor" in initial["detail"]
        # The live stop raise is marked current
        raised = next(e for e in events if e["label"] == "Stop raised")
        assert "$32.71" in raised["detail"]
        assert "current" in raised["detail"]

    def test_events_sorted_chronologically(self):
        events = _build_lifecycle_events(_achc_pos(), _achc_history())
        stamps = [e["ts"] for e in events if e["ts"]]
        assert stamps == sorted(stamps)

    def test_cancelled_entry_shows_pivot_never_triggered(self):
        entry = _order(
            oid="entry", side="buy", order_type="stop", status="canceled",
            stop_price=7.22, qty=108,
        )
        entry["canceled_at"] = "2026-07-17T19:45:01Z"
        pos = _achc_pos(
            symbol="ATAI", status="CANCELLED", entry_order_id="entry",
            close_reason="ENTRY_NEVER_FILLED", closed_at="2026-07-17T19:45:02Z",
            entry_price=7.22, entry_orl=3.96, current_stop=3.96,
        )
        events = _build_lifecycle_events(pos, [entry])
        labels = [e["label"] for e in events]
        assert "Entry submitted" in labels
        assert "Entry cancelled" in labels
        assert "Entry filled" not in labels

    def test_superseded_stops_labelled_as_history(self):
        history = _achc_history()
        history.insert(0, _order(
            oid="s-old", side="sell", order_type="stop", status="canceled",
            submitted_at="2026-07-17T16:00:00Z", stop_price=30.00,
        ))
        events = _build_lifecycle_events(_achc_pos(), history)
        raises = [e for e in events if e["label"] == "Stop raised"]
        assert len(raises) == 2
        assert any("superseded" in e["detail"] for e in raises)
        assert any("current" in e["detail"] for e in raises)

    def test_trim_and_close_events(self):
        pos = _achc_pos(
            trimmed=1, trim_date="2026-07-21", current_qty=17,
            status="CLOSED", closed_at="2026-07-24T20:00:00Z",
            close_reason="TRAIL_10SMA",
        )
        events = _build_lifecycle_events(pos, _achc_history())
        labels = [e["label"] for e in events]
        assert "Trimmed" in labels
        assert "Closed" in labels
        closed = next(e for e in events if e["label"] == "Closed")
        assert closed["detail"] == "TRAIL_10SMA"

    def test_db_only_fallback_without_orders(self):
        events = _build_lifecycle_events(_achc_pos(entry_order_id=None,
                                                   stop_order_id=None), [])
        labels = [e["label"] for e in events]
        assert "Opened" in labels
        assert "Initial stop set" in labels
