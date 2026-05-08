"""Alpaca Market Data wrapper for screening and historical bar retrieval.

Uses ``StockHistoricalDataClient`` for bars/snapshots and
``ScreenerClient`` for the most-active-stocks screener endpoint.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta
from typing import Optional

import pandas as pd
from alpaca.data.historical import ScreenerClient, StockHistoricalDataClient
from alpaca.data.requests import (
    MostActivesRequest,
    StockBarsRequest,
    StockSnapshotRequest,
)
from alpaca.data.timeframe import TimeFrame

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data client wrapper
# ---------------------------------------------------------------------------

class AlpacaDataClient:
    """Wrapper around Alpaca's market-data and screener APIs.

    Parameters
    ----------
    api_key : str, optional
        Falls back to ``ALPACA_API_KEY``.
    secret_key : str, optional
        Falls back to ``ALPACA_SECRET_KEY``.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        secret_key: Optional[str] = None,
    ):
        self._api_key = api_key or os.environ.get("ALPACA_API_KEY", "")
        self._secret_key = secret_key or os.environ.get("ALPACA_SECRET_KEY", "")
        self._stock_client: Optional[StockHistoricalDataClient] = None
        self._screener_client: Optional[ScreenerClient] = None

    # -- lazy init ----------------------------------------------------------

    @property
    def stock_client(self) -> StockHistoricalDataClient:
        if self._stock_client is None:
            self._stock_client = StockHistoricalDataClient(
                self._api_key, self._secret_key
            )
        return self._stock_client

    @property
    def screener_client(self) -> ScreenerClient:
        if self._screener_client is None:
            self._screener_client = ScreenerClient(
                self._api_key, self._secret_key
            )
        return self._screener_client

    # -- screener -----------------------------------------------------------

    def get_most_active(self, top: int = 20, by: str = "volume") -> list[dict]:
        """Return the most-active tickers from Alpaca's screener.

        Parameters
        ----------
        top : int
            Number of results to return.
        by : str
            Ranking criterion -- ``"volume"`` or ``"trades"``.

        Returns
        -------
        list[dict]
            Each dict contains ``symbol``, ``volume``, ``trade_count``.
        """
        request = MostActivesRequest(by=by, top=top)
        response = self.screener_client.get_most_actives(request)

        results = []
        for item in response.most_actives:
            results.append(
                {
                    "symbol": item.symbol,
                    "volume": item.volume,
                    "trade_count": item.trade_count,
                }
            )
        logger.info("Screener returned %d most-active tickers", len(results))
        return results

    # -- snapshots ----------------------------------------------------------

    def get_snapshots(self, symbols: list[str]) -> dict:
        """Return latest snapshots (price, daily bar, volume) for *symbols*.

        Returns
        -------
        dict
            Mapping of ``symbol -> snapshot`` objects.  Each snapshot has
            attributes ``latest_trade``, ``latest_quote``, ``minute_bar``,
            ``daily_bar``, ``previous_daily_bar``.
        """
        request = StockSnapshotRequest(symbol_or_symbols=symbols)
        return self.stock_client.get_stock_snapshot(request)

    # -- bars ---------------------------------------------------------------

    def get_bars(
        self,
        symbols: list[str] | str,
        timeframe: TimeFrame = TimeFrame.Day,
        start: Optional[datetime] = None,
        end: Optional[datetime] = None,
        lookback_days: int = 60,
    ) -> pd.DataFrame:
        """Fetch historical bars and return a ``DataFrame``.

        Parameters
        ----------
        symbols : list[str] or str
            One or more ticker symbols.
        timeframe : TimeFrame
            Bar resolution (default daily).
        start : datetime, optional
            Start date.  Defaults to *lookback_days* ago.
        end : datetime, optional
            End date.  Defaults to now.
        lookback_days : int
            Used when *start* is not given.

        Returns
        -------
        pd.DataFrame
            Multi-indexed by ``(symbol, timestamp)`` with columns
            ``open``, ``high``, ``low``, ``close``, ``volume``, ``vwap``,
            ``trade_count``.
        """
        if isinstance(symbols, str):
            symbols = [symbols]
        if start is None:
            start = datetime.now() - timedelta(days=lookback_days)
        if end is None:
            end = datetime.now()

        request = StockBarsRequest(
            symbol_or_symbols=symbols,
            timeframe=timeframe,
            start=pd.Timestamp(start, tz="America/New_York"),
            end=pd.Timestamp(end, tz="America/New_York"),
        )
        bars = self.stock_client.get_stock_bars(request)
        return bars.df

    # -- convenience: technical helpers -------------------------------------

    def compute_sma(
        self,
        symbol: str,
        period: int = 10,
        lookback_days: int = 60,
    ) -> pd.Series:
        """Compute the Simple Moving Average for *symbol*.

        Returns a ``Series`` indexed by date with the SMA values.
        """
        df = self.get_bars(symbol, lookback_days=lookback_days)
        if df.empty:
            return pd.Series(dtype=float)
        # If multi-indexed, get just this symbol
        if isinstance(df.index, pd.MultiIndex):
            df = df.xs(symbol, level="symbol")
        return df["close"].rolling(window=period).mean()

    def compute_adr_pct(
        self,
        symbol: str,
        period: int = 14,
        lookback_days: int = 60,
    ) -> float:
        """Compute the Average Daily Range as a percentage of price.

        ADR% = mean((high - low) / close) over *period* days.
        """
        df = self.get_bars(symbol, lookback_days=lookback_days)
        if df.empty:
            return 0.0
        if isinstance(df.index, pd.MultiIndex):
            df = df.xs(symbol, level="symbol")
        daily_range_pct = (df["high"] - df["low"]) / df["close"]
        return float(daily_range_pct.tail(period).mean())

    def compute_relative_strength(
        self,
        symbol: str,
        benchmark: str = "SPY",
        period: int = 20,
        lookback_days: int = 60,
    ) -> float:
        """Compute the relative performance of *symbol* vs *benchmark*.

        Returns the difference: ``symbol_return - benchmark_return`` over
        *period* trading days.  Positive means outperforming.
        """
        df = self.get_bars([symbol, benchmark], lookback_days=lookback_days)
        if df.empty:
            return 0.0

        result = {}
        for sym in [symbol, benchmark]:
            try:
                sym_df = df.xs(sym, level="symbol")
            except KeyError:
                return 0.0
            if len(sym_df) < period:
                return 0.0
            close = sym_df["close"]
            pct_return = (close.iloc[-1] - close.iloc[-period]) / close.iloc[-period]
            result[sym] = pct_return

        return result.get(symbol, 0.0) - result.get(benchmark, 0.0)

    def get_dollar_volume(
        self,
        symbol: str,
        period: int = 20,
        lookback_days: int = 60,
    ) -> float:
        """Return the average daily dollar volume over *period* days."""
        df = self.get_bars(symbol, lookback_days=lookback_days)
        if df.empty:
            return 0.0
        if isinstance(df.index, pd.MultiIndex):
            df = df.xs(symbol, level="symbol")
        dollar_vol = df["close"] * df["volume"]
        return float(dollar_vol.tail(period).mean())
