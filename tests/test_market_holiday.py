"""Tests for market holiday awareness (is_trading_day).

Verifies that:
- A normal trading day is correctly identified
- A market holiday is correctly identified
- API failures fail-open (assume trading day)
- pre_market() returns an empty context on holidays
- execute_entries() aborts on holidays
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import date
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Minimal stubs so we can import DailyWorkflow without real Alpaca creds
# ---------------------------------------------------------------------------

@dataclass
class _FakeCalendarEntry:
    """Mimics the Alpaca CalendarEntry object."""
    date: date
    open: str = "09:30"
    close: str = "16:00"


@pytest.fixture
def workflow():
    """Create a DailyWorkflow with mocked components."""
    from tradingagents.daemon.daily_workflow import DailyWorkflow

    wf = DailyWorkflow()
    # Inject mocks so _ensure_components doesn't hit real APIs
    wf._alpaca_client = MagicMock()
    wf._data_client = MagicMock()
    wf._trade_db = MagicMock()
    return wf


# ---------------------------------------------------------------------------
# is_trading_day() tests
# ---------------------------------------------------------------------------

class TestIsTradingDay:
    """Tests for DailyWorkflow.is_trading_day()."""

    def test_normal_trading_day(self, workflow):
        """Calendar returns an entry matching the requested date → True."""
        target = date(2026, 5, 26)  # Tuesday — normal trading day
        workflow._alpaca_client.get_calendar.return_value = [
            _FakeCalendarEntry(date=target)
        ]

        assert workflow.is_trading_day(target) is True

    def test_market_holiday(self, workflow):
        """Calendar returns the next trading day, not the requested date → False."""
        holiday = date(2026, 5, 25)  # Memorial Day
        next_day = date(2026, 5, 26)
        workflow._alpaca_client.get_calendar.return_value = [
            _FakeCalendarEntry(date=next_day)
        ]

        assert workflow.is_trading_day(holiday) is False

    def test_empty_calendar_response(self, workflow):
        """Empty calendar list → assume NOT a trading day."""
        workflow._alpaca_client.get_calendar.return_value = []

        assert workflow.is_trading_day(date(2026, 7, 4)) is False

    def test_api_failure_fails_open(self, workflow):
        """API exception → fail-open, assume IS a trading day."""
        workflow._alpaca_client.get_calendar.side_effect = Exception(
            "connection timeout"
        )

        assert workflow.is_trading_day(date(2026, 5, 25)) is True

    def test_calendar_entry_with_string_date(self, workflow):
        """Calendar entry where .date is a string, not a date object."""
        target = date(2026, 5, 26)
        entry = SimpleNamespace(date="2026-05-26")
        workflow._alpaca_client.get_calendar.return_value = [entry]

        assert workflow.is_trading_day(target) is True

    def test_weekend(self, workflow):
        """Saturday — calendar returns Monday → False."""
        saturday = date(2026, 5, 23)
        monday = date(2026, 5, 25)  # (this is actually Memorial Day too)
        workflow._alpaca_client.get_calendar.return_value = [
            _FakeCalendarEntry(date=monday)
        ]

        assert workflow.is_trading_day(saturday) is False


# ---------------------------------------------------------------------------
# pre_market() holiday gating tests
# ---------------------------------------------------------------------------

class TestPreMarketHolidayGate:
    """Tests that pre_market() skips on holidays."""

    @patch("tradingagents.daemon.daily_workflow.notify")
    def test_pre_market_skips_on_holiday(self, mock_notify, workflow):
        """pre_market() returns empty context and sends notification on holiday."""
        workflow._alpaca_client.get_calendar.return_value = [
            _FakeCalendarEntry(date=date(2026, 5, 26))  # next day, not today
        ]

        with patch("tradingagents.daemon.daily_workflow.date") as mock_date:
            mock_date.today.return_value = date(2026, 5, 25)
            mock_date.side_effect = lambda *a, **kw: date(*a, **kw)
            ctx = workflow.pre_market()

        # Should return context with no candidates
        assert ctx.candidates == []
        assert ctx.pipeline_decisions == {}

        # Should have sent a notification
        mock_notify.assert_called()
        call_args = mock_notify.call_args
        assert call_args[0][0] == "info"
        assert "holiday" in call_args[1]["message"].lower()

    @patch("tradingagents.daemon.daily_workflow.notify")
    @patch("tradingagents.daemon.daily_workflow.date")
    def test_pre_market_proceeds_on_trading_day(
        self, mock_date_cls, mock_notify, workflow
    ):
        """pre_market() proceeds normally on a real trading day.

        We mock just enough to verify the holiday gate doesn't fire.
        The rest of pre_market will fail (no real data), which is fine —
        we're only testing the gate.
        """
        today = date(2026, 5, 26)
        mock_date_cls.today.return_value = today
        mock_date_cls.side_effect = lambda *a, **kw: date(*a, **kw)

        workflow._alpaca_client.get_calendar.return_value = [
            _FakeCalendarEntry(date=today)
        ]

        # pre_market will proceed past the holiday check and hit
        # check_market_regime which will fail — that's fine, we just
        # need to confirm the holiday notification was NOT sent.
        try:
            workflow.pre_market()
        except Exception:
            pass  # expected — downstream code needs real data

        # The holiday notification should NOT have been sent
        for call in mock_notify.call_args_list:
            if call[0][0] == "info":
                assert "holiday" not in call[1].get("message", "").lower()


# ---------------------------------------------------------------------------
# execute_entries() holiday gating tests
# ---------------------------------------------------------------------------

class TestExecuteEntriesHolidayGate:

    @patch("tradingagents.daemon.daily_workflow.notify")
    def test_execute_entries_aborts_on_holiday(self, mock_notify, workflow):
        """execute_entries() returns early on a non-trading day."""
        from tradingagents.daemon.daily_workflow import DayContext

        # Simulate a context that would normally proceed
        workflow._ctx = DayContext(
            date="2026-05-25",
            regime_favorable=True,
            pipeline_decisions={"IREN": "buy"},
        )
        workflow._alpaca_client.get_calendar.return_value = [
            _FakeCalendarEntry(date=date(2026, 5, 26))  # not today
        ]

        with patch("tradingagents.daemon.daily_workflow.date") as mock_date:
            mock_date.today.return_value = date(2026, 5, 25)
            mock_date.side_effect = lambda *a, **kw: date(*a, **kw)
            ctx = workflow.execute_entries()

        # Should return without submitting any entries
        assert ctx.entries_submitted == []
