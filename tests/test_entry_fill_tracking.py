"""Tests for entry fill tracking: PENDING positions and reconciliation.

An entry order (especially a buy-stop at the pivot) may never fill, so
positions are recorded as PENDING at submission and only promoted to
OPEN — with the actual fill price — once Alpaca confirms the fill.

Covers:
  - TradeDB lifecycle: PENDING -> OPEN (fill) / CANCELLED (never filled)
  - update_stop writes current_stop, never touches entry_orl
  - get_open_positions include_pending semantics
  - DailyWorkflow._reconcile_pending_entries state transitions
  - Guardrails count PENDING entries toward the position cap
  - PositionManager ignores PENDING entries
"""

import os
from datetime import datetime
from types import SimpleNamespace

import pytest

from tradingagents.execution.trade_db import TradeDB


@pytest.fixture
def db(tmp_path):
    return TradeDB(db_path=str(tmp_path / "test_trades.db"))


def _submit_entry(db, symbol="ATAI", order_id="order-1", **overrides):
    """Mirror what Executor.execute_entry records at submission."""
    kwargs = dict(
        symbol=symbol,
        entry_date="2026-07-17",
        entry_price=7.22,
        entry_orl=3.96,
        qty=108,
        stop_order_id=order_id,
        entry_order_id=order_id,
        pipeline_mode="quant",
    )
    kwargs.update(overrides)
    db.open_position(**kwargs)
    if order_id:
        db.record_order(
            symbol=symbol,
            side="buy",
            qty=kwargs["qty"],
            order_type="bracket",
            limit_price=kwargs["entry_price"],
            stop_price=kwargs["entry_orl"],
            order_id=order_id,
            status="submitted",
        )


# ---------------------------------------------------------------------------
# TradeDB lifecycle
# ---------------------------------------------------------------------------

class TestPositionLifecycle:
    def test_new_entry_is_pending_not_open(self, db):
        _submit_entry(db)
        pos = db.get_position("ATAI")
        assert pos["status"] == "PENDING"
        assert pos["entry_order_id"] == "order-1"
        assert pos["pipeline_mode"] == "quant"
        # current_stop starts at the initial stop
        assert pos["current_stop"] == pos["entry_orl"] == 3.96
        # Not in the default open list...
        assert db.get_open_positions() == []
        # ...but visible for slot counting
        assert len(db.get_open_positions(include_pending=True)) == 1
        assert len(db.get_pending_positions()) == 1

    def test_mark_filled_promotes_with_actual_fill(self, db):
        _submit_entry(db, entry_price=33.62)
        db.mark_position_filled("ATAI", fill_price=33.27, fill_date="2026-07-17")
        pos = db.get_position("ATAI")
        assert pos["status"] == "OPEN"
        assert pos["entry_price"] == 33.27  # real fill, not signal price
        assert len(db.get_open_positions()) == 1
        assert db.get_pending_positions() == []

    def test_partial_fill_adjusts_quantity(self, db):
        _submit_entry(db, qty=108)
        db.mark_position_filled("ATAI", fill_price=7.25, fill_qty=50)
        pos = db.get_position("ATAI")
        assert pos["current_qty"] == 50
        assert pos["original_qty"] == 50

    def test_cancel_pending_retires_quietly(self, db):
        _submit_entry(db)
        db.cancel_pending_position("ATAI")
        pos = db.get_position("ATAI")
        assert pos["status"] == "CANCELLED"
        assert pos["close_reason"] == "ENTRY_NEVER_FILLED"
        assert db.get_open_positions(include_pending=True) == []
        # Not counted as a closed trade either
        assert all(p["symbol"] != "ATAI" for p in db.get_closed_positions())

    def test_cancel_only_touches_pending(self, db):
        _submit_entry(db)
        db.mark_position_filled("ATAI", fill_price=7.25)
        db.cancel_pending_position("ATAI")  # must be a no-op on OPEN
        assert db.get_position("ATAI")["status"] == "OPEN"

    def test_reentry_after_cancel_resets_row(self, db):
        _submit_entry(db)
        db.cancel_pending_position("ATAI")
        _submit_entry(db, order_id="order-2", entry_price=8.10)
        pos = db.get_position("ATAI")
        assert pos["status"] == "PENDING"
        assert pos["entry_order_id"] == "order-2"
        assert pos["close_reason"] is None
        assert pos["closed_at"] is None


class TestStopSeparation:
    def test_update_stop_preserves_initial_stop(self, db):
        _submit_entry(db, symbol="ACHC", entry_orl=23.79)
        db.mark_position_filled("ACHC", fill_price=33.27)

        db.update_stop("ACHC", 32.71, stop_type="lod")
        pos = db.get_position("ACHC")
        assert pos["entry_orl"] == 23.79     # immutable initial stop
        assert pos["current_stop"] == 32.71  # live stop moved

        db.update_stop("ACHC", 34.00, stop_type="trailing")
        pos = db.get_position("ACHC")
        assert pos["entry_orl"] == 23.79
        assert pos["current_stop"] == 34.00
        assert pos["trailing_stop_active"] == 1

    def test_legacy_rows_backfill_current_stop(self, db, tmp_path):
        _submit_entry(db, symbol="OLD", entry_orl=10.0)
        # Simulate a pre-migration row (no current_stop)
        db.update_position("OLD", current_stop=None)
        # Re-init on the same file re-runs migrations
        db2 = TradeDB(db_path=str(tmp_path / "test_trades.db"))
        assert db2.get_position("OLD")["current_stop"] == 10.0


# ---------------------------------------------------------------------------
# DailyWorkflow reconciliation
# ---------------------------------------------------------------------------

def _order(status, filled_qty=0, filled_avg_price=None, filled_at=None):
    return SimpleNamespace(
        status=status,
        filled_qty=str(filled_qty),
        filled_avg_price=str(filled_avg_price) if filled_avg_price else None,
        filled_at=filled_at,
    )


class StubAlpaca:
    def __init__(self, orders):
        self.orders = orders  # order_id -> order object
        self.cancelled = []

    def get_order_by_id(self, order_id):
        return self.orders[order_id]

    def cancel_order(self, order_id):
        self.cancelled.append(order_id)


def _workflow(db, alpaca):
    from tradingagents.daemon.daily_workflow import DailyWorkflow

    wf = DailyWorkflow(config={})
    wf._trade_db = db
    wf._alpaca_client = alpaca
    return wf


class TestReconcilePendingEntries:
    def test_filled_order_promotes_position(self, db):
        _submit_entry(db, symbol="ACHC", order_id="o1", entry_price=33.62)
        alpaca = StubAlpaca({"o1": _order(
            "filled", filled_qty=35, filled_avg_price=33.27,
            filled_at=datetime(2026, 7, 17, 9, 30),
        )})

        _workflow(db, alpaca)._reconcile_pending_entries()

        pos = db.get_position("ACHC")
        assert pos["status"] == "OPEN"
        assert pos["entry_price"] == 33.27
        assert pos["entry_date"] == "2026-07-17"
        # DB order row synced too
        order_row = db.get_orders_for_symbol("ACHC")[0]
        assert order_row["status"] == "FILLED"
        assert order_row["filled_price"] == 33.27

    def test_cancelled_order_retires_position(self, db):
        _submit_entry(db, symbol="ATAI", order_id="o1")
        alpaca = StubAlpaca({"o1": _order("canceled")})

        _workflow(db, alpaca)._reconcile_pending_entries()

        assert db.get_position("ATAI")["status"] == "CANCELLED"
        assert db.get_orders_for_symbol("ATAI")[0]["status"] == "CANCELLED"

    def test_working_order_stays_pending_intraday(self, db):
        _submit_entry(db, order_id="o1")
        alpaca = StubAlpaca({"o1": _order("new")})

        _workflow(db, alpaca)._reconcile_pending_entries()

        assert db.get_position("ATAI")["status"] == "PENDING"
        assert alpaca.cancelled == []

    def test_eod_cancels_unfilled_entries(self, db):
        _submit_entry(db, order_id="o1")
        alpaca = StubAlpaca({"o1": _order("new")})

        _workflow(db, alpaca)._reconcile_pending_entries(cancel_unfilled=True)

        assert alpaca.cancelled == ["o1"]
        assert db.get_position("ATAI")["status"] == "CANCELLED"

    def test_enum_style_status_is_normalized(self, db):
        _submit_entry(db, order_id="o1")
        alpaca = StubAlpaca({"o1": _order("OrderStatus.CANCELED")})

        _workflow(db, alpaca)._reconcile_pending_entries()

        assert db.get_position("ATAI")["status"] == "CANCELLED"

    def test_legacy_row_without_order_id_assumed_filled(self, db):
        _submit_entry(db, order_id=None, stop_order_id=None, entry_order_id=None)
        alpaca = StubAlpaca({})

        _workflow(db, alpaca)._reconcile_pending_entries()

        assert db.get_position("ATAI")["status"] == "OPEN"

    def test_api_error_leaves_position_pending(self, db):
        _submit_entry(db, order_id="o1")

        class BrokenAlpaca:
            def get_order_by_id(self, order_id):
                raise RuntimeError("api down")

        _workflow(db, BrokenAlpaca())._reconcile_pending_entries()

        assert db.get_position("ATAI")["status"] == "PENDING"


# ---------------------------------------------------------------------------
# Guardrails and PositionManager semantics
# ---------------------------------------------------------------------------

class TestSlotSemantics:
    def test_guardrails_count_pending_toward_cap(self, db):
        from tradingagents.execution.guardrails import Guardrails

        from tradingagents.strategies.swing_playbook import get_sizing_params

        # Fill every slot with PENDING entries -- an unfilled buy-stop must
        # still reserve one, or N submitted stops could all trigger at once.
        cap = get_sizing_params()["max_concurrent_positions"]
        for i in range(cap):
            _submit_entry(db, symbol=f"SYM{i}", order_id=f"o{i}")

        gr = Guardrails(alpaca_client=None, trade_db=db)
        result = gr.validate_entry("NEWSYM", proposed_value=1000)
        assert not result.approved
        assert "max positions" in result.reason

    def test_guardrails_reject_symbol_with_pending_entry(self, db):
        from tradingagents.execution.guardrails import Guardrails

        _submit_entry(db, symbol="ATAI")
        gr = Guardrails(alpaca_client=None, trade_db=db)
        result = gr.validate_entry("ATAI", proposed_value=1000)
        assert not result.approved
        assert "already holding" in result.reason

    def test_position_manager_ignores_pending(self, db):
        from tradingagents.execution.position_manager import PositionManager

        _submit_entry(db, symbol="ATAI")  # PENDING — must not be evaluated

        class ExplodingDataClient:
            def get_bars(self, *a, **kw):
                raise AssertionError("data fetched for a PENDING position")

        pm = PositionManager(data_client=ExplodingDataClient(), trade_db=db)
        assert pm.evaluate_all() == []
