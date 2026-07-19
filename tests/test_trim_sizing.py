"""Whole-share trim behaviour on small accounts.

A 50% trim of a 1-share position rounds to 0. That must be a true no-op that
leaves the protective stop untouched -- not a path that cancels the bracket
stop for a sale which never happens (which left the position unprotected until
post_market and re-fired every day).  A real trim must re-protect the
remaining shares immediately.
"""

from types import SimpleNamespace

import pandas as pd
import pytest

from tradingagents.daemon.daily_workflow import DailyWorkflow, DayContext
from tradingagents.execution.position_manager import PositionAction, PositionManager
from tradingagents.execution.trade_db import TradeDB


# ---------------------------------------------------------------------------
# Position-manager guard: don't emit a trim that can't sell a whole share
# ---------------------------------------------------------------------------

class _StubData:
    """Day-3, profitable, non-extended, trailing-inactive snapshot so the
    trim rule (Rule 3) is the one under test."""

    def __init__(self, close=12.0, low=11.5, sma10=9.0, sma50=11.0, adr=0.05):
        self._c, self._l, self._s10, self._s50, self._adr = close, low, sma10, sma50, adr

    def get_bars(self, symbol, lookback_days=30):
        return pd.DataFrame(
            {"close": [self._c], "low": [self._l]},
            index=pd.date_range("2026-07-17", periods=1),
        )

    def compute_adr_pct(self, symbol, period=14):
        return self._adr

    def compute_sma(self, symbol, period=10):
        return pd.Series([self._s10 if period == 10 else self._s50])


class _StubDB:
    def __init__(self, positions):
        self._positions = positions

    def get_open_positions(self, include_pending=False):
        return self._positions


def _pos(qty, **over):
    p = dict(symbol="AAA", entry_price=10.0, current_stop=9.0,
             day_count=3, trimmed=0, current_qty=qty)
    p.update(over)
    return p


def test_one_share_winner_is_not_trimmed():
    pm = PositionManager(data_client=_StubData(), trade_db=_StubDB([_pos(1)]))
    action = pm.evaluate_all()[0]
    assert action.action != "exit_partial"  # floor(1 * 50%) = 0 -> hold


def test_two_share_winner_is_trimmed():
    pm = PositionManager(data_client=_StubData(), trade_db=_StubDB([_pos(2)]))
    action = pm.evaluate_all()[0]
    assert action.action == "exit_partial"  # floor(2 * 50%) = 1


def test_three_share_winner_is_trimmed():
    pm = PositionManager(data_client=_StubData(), trade_db=_StubDB([_pos(3)]))
    action = pm.evaluate_all()[0]
    assert action.action == "exit_partial"
    assert action.exit_pct == 0.5


# ---------------------------------------------------------------------------
# _execute_exit: no-op safety and remainder re-protection
# ---------------------------------------------------------------------------

class _FakeAlpaca:
    def __init__(self, qty, stop_order_id="stop-1"):
        self._qty = float(qty)
        self.cancelled = []
        self.closed = []   # (symbol, qty)
        self.stops = []    # (symbol, qty, stop_price)
        self._open = [SimpleNamespace(id=stop_order_id, type="stop")] if stop_order_id else []

    def get_orders(self, symbols=None):
        return list(self._open)

    def cancel_order(self, oid):
        self.cancelled.append(str(oid))
        self._open = [o for o in self._open if str(o.id) != str(oid)]

    def get_position(self, symbol):
        return SimpleNamespace(symbol=symbol, qty=str(self._qty)) if self._qty > 0 else None

    def close_position(self, symbol, qty=None):
        self.closed.append((symbol, qty))
        if qty:
            self._qty -= float(qty)

    def submit_stop_order(self, symbol, qty, side, stop_price, **kw):
        self.stops.append((symbol, float(qty), float(stop_price)))
        return SimpleNamespace(id="newstop-1")


@pytest.fixture
def db(tmp_path):
    return TradeDB(db_path=str(tmp_path / "trades.db"))


def _open_position(db, symbol, qty, stop=9.0):
    db.open_position(
        symbol=symbol, entry_date="2026-07-16", entry_price=10.0,
        entry_orl=stop, qty=qty, stop_order_id="stop-1",
        entry_order_id="stop-1", pipeline_mode="quant",
    )


def _workflow(alpaca, db):
    wf = DailyWorkflow()
    wf._alpaca_client = alpaca
    wf._trade_db = db
    wf._ctx = DayContext(date="2026-07-19")
    return wf


@pytest.fixture(autouse=True)
def _silence(monkeypatch):
    monkeypatch.setattr("tradingagents.daemon.daily_workflow.notify", lambda *a, **k: None)
    monkeypatch.setattr("time.sleep", lambda *a, **k: None)


def test_one_share_trim_is_noop_and_keeps_stop(db):
    _open_position(db, "AAA", qty=1)
    fake = _FakeAlpaca(qty=1)
    wf = _workflow(fake, db)

    wf._execute_exit(PositionAction("AAA", "exit_partial", "day 3 trim", exit_pct=0.5))

    assert fake.cancelled == []                       # stop never cancelled
    assert fake.closed == []                          # nothing sold
    assert fake.stops == []                           # no new stop churned
    assert not db.get_position("AAA").get("trimmed")  # not marked trimmed
    assert wf._ctx.exits_executed == []               # no phantom exit recorded


def test_three_share_trim_sells_one_and_reprotects(db):
    _open_position(db, "BBB", qty=3, stop=9.0)
    fake = _FakeAlpaca(qty=3)
    wf = _workflow(fake, db)

    wf._execute_exit(PositionAction("BBB", "exit_partial", "day 3 trim", exit_pct=0.5))

    assert fake.cancelled == ["stop-1"]        # cancelled the bracket stop
    assert fake.closed == [("BBB", 1)]         # sold floor(3 * 50%) = 1
    assert fake.stops == [("BBB", 2.0, 9.0)]   # re-protected remaining 2 @ $9
    assert db.get_position("BBB").get("trimmed") == 1


def test_partial_with_no_live_position_is_safe(db):
    _open_position(db, "CCC", qty=2)
    fake = _FakeAlpaca(qty=0)  # position already gone on Alpaca
    wf = _workflow(fake, db)

    wf._execute_exit(PositionAction("CCC", "exit_partial", "day 3 trim", exit_pct=0.5))

    assert fake.cancelled == []
    assert fake.closed == []
    assert fake.stops == []
