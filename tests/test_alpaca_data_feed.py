"""Tests for AlpacaDataClient feed selection and SIP delay handling.

The daily-bar feed defaults to SIP (consolidated tape) because IEX bars
only contain IEX-exchange trades -- ~3-5% of real volume -- which breaks
the pre-filter's dollar-volume and ADR gates.  Free-plan SIP access
requires requests to end 15+ minutes in the past, so get_bars clamps
its end time.
"""

from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest
from alpaca.data.enums import DataFeed

from tradingagents.execution.alpaca_data import AlpacaDataClient, _resolve_feed


class TestResolveFeed:
    def test_defaults(self, monkeypatch):
        monkeypatch.delenv("ALPACA_DATA_FEED", raising=False)
        monkeypatch.delenv("ALPACA_DAILY_FEED", raising=False)
        assert _resolve_feed("ALPACA_DATA_FEED", "iex") == DataFeed.IEX
        assert _resolve_feed("ALPACA_DAILY_FEED", "sip") == DataFeed.SIP

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv("ALPACA_DAILY_FEED", "iex")
        assert _resolve_feed("ALPACA_DAILY_FEED", "sip") == DataFeed.IEX

    def test_invalid_value_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv("ALPACA_DAILY_FEED", "bogus")
        assert _resolve_feed("ALPACA_DAILY_FEED", "sip") == DataFeed.SIP


class TestGetBarsFeedHandling:
    def _client_with_mock(self, monkeypatch, daily_feed="sip"):
        monkeypatch.setenv("ALPACA_DAILY_FEED", daily_feed)
        client = AlpacaDataClient(api_key="k", secret_key="s")
        mock_stock = MagicMock()
        mock_stock.get_stock_bars.return_value = MagicMock(df=pd.DataFrame())
        client._stock_client = mock_stock
        return client, mock_stock

    def test_sip_clamps_end_time(self, monkeypatch):
        client, mock_stock = self._client_with_mock(monkeypatch, "sip")
        client.get_bars("AAPL", lookback_days=30)

        request = mock_stock.get_stock_bars.call_args[0][0]
        assert request.feed == DataFeed.SIP
        # StockBarsRequest normalizes timestamps to naive UTC
        clamp = pd.Timestamp.utcnow().tz_localize(None) - timedelta(minutes=15)
        assert request.end <= clamp

    def test_iex_does_not_clamp_end_time(self, monkeypatch):
        client, mock_stock = self._client_with_mock(monkeypatch, "iex")
        client.get_bars("AAPL", lookback_days=30)

        request = mock_stock.get_stock_bars.call_args[0][0]
        assert request.feed == DataFeed.IEX
        # StockBarsRequest normalizes timestamps to naive UTC
        floor = pd.Timestamp.utcnow().tz_localize(None) - timedelta(minutes=1)
        assert request.end >= floor

    def test_sip_subscription_error_falls_back_to_iex(self, monkeypatch):
        client, mock_stock = self._client_with_mock(monkeypatch, "sip")
        mock_stock.get_stock_bars.side_effect = [
            RuntimeError("subscription does not permit querying recent SIP data"),
            MagicMock(df=pd.DataFrame()),
        ]
        client.get_bars("AAPL", lookback_days=30)

        assert mock_stock.get_stock_bars.call_count == 2
        retry_request = mock_stock.get_stock_bars.call_args[0][0]
        assert retry_request.feed == DataFeed.IEX
        # Downgrade is sticky for subsequent calls
        assert client._daily_feed == DataFeed.IEX

    def test_non_subscription_error_propagates(self, monkeypatch):
        client, mock_stock = self._client_with_mock(monkeypatch, "sip")
        mock_stock.get_stock_bars.side_effect = RuntimeError("rate limit exceeded")
        with pytest.raises(RuntimeError, match="rate limit"):
            client.get_bars("AAPL", lookback_days=30)
