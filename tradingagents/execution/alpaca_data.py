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
from alpaca.data.enums import DataFeed
from alpaca.data.timeframe import TimeFrame

logger = logging.getLogger(__name__)


def _resolve_feed(env_var: str, default: str) -> DataFeed:
    """Resolve a market-data feed name from an environment variable."""
    raw = os.environ.get(env_var, default).strip().lower()
    try:
        return DataFeed(raw)
    except ValueError:
        logger.warning(
            "Invalid %s=%r -- falling back to %s", env_var, raw, default
        )
        return DataFeed(default)


# ---------------------------------------------------------------------------
# Data client wrapper
# ---------------------------------------------------------------------------

class AlpacaDataClient:
    """Wrapper around Alpaca's market-data and screener APIs.

    Two feeds are used:

    - **Daily/historical bars** (``get_bars`` and everything built on it)
      default to the SIP feed (``ALPACA_DAILY_FEED``).  IEX bars only
      contain IEX-exchange trades (~3-5% of consolidated volume), which
      understates dollar volume ~20x and narrows high/low ranges (ADR).
      The free plan allows SIP data delayed 15+ minutes, so daily-bar
      requests are clamped to end 16 minutes in the past.
    - **Real-time data** (``get_snapshots``, ``get_intraday_bars``)
      defaults to IEX (``ALPACA_DATA_FEED``), which is free without
      delay.  Set to ``sip`` with a market-data subscription.

    Parameters
    ----------
    api_key : str, optional
        Falls back to ``ALPACA_API_KEY``.
    secret_key : str, optional
        Falls back to ``ALPACA_SECRET_KEY``.
    """

    # Free-plan SIP data must trail real time by 15 minutes; use 16 for slack.
    _SIP_DELAY_MINUTES = 16

    def __init__(
        self,
        api_key: Optional[str] = None,
        secret_key: Optional[str] = None,
    ):
        self._api_key = api_key or os.environ.get("ALPACA_API_KEY", "")
        self._secret_key = secret_key or os.environ.get("ALPACA_SECRET_KEY", "")
        self._feed = _resolve_feed("ALPACA_DATA_FEED", "iex")
        self._daily_feed = _resolve_feed("ALPACA_DAILY_FEED", "sip")
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

    def get_market_movers(self, top: int = 20) -> list[dict]:
        """Return the top *top* gainers from Alpaca's market-movers screener.

        Gainers align with the swing playbook's momentum bias far better
        than most-actives-by-volume (which surfaces mega-caps and
        sub-$5 volume churners).

        Returns
        -------
        list[dict]
            Each dict contains ``symbol``, ``price``, ``change``,
            ``percent_change``, sorted by percent change descending.
        """
        from alpaca.data.requests import MarketMoversRequest

        request = MarketMoversRequest(top=top)
        response = self.screener_client.get_market_movers(request)

        results = []
        for item in response.gainers:
            results.append(
                {
                    "symbol": item.symbol,
                    "price": float(item.price),
                    "change": float(item.change),
                    "percent_change": float(item.percent_change),
                }
            )
        logger.info("Screener returned %d market-mover gainers", len(results))
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
        request = StockSnapshotRequest(symbol_or_symbols=symbols, feed=self._feed)
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

        # Free-plan SIP access requires the window to end 15+ min in the past.
        # Only affects the current day's partial bar -- harmless for daily data.
        # Naive end times are interpreted as New York wall time below, so the
        # clamp is computed on the NY clock (machine-local time may differ).
        if self._daily_feed == DataFeed.SIP and end.tzinfo is None:
            ny_now = pd.Timestamp.now(tz="America/New_York").tz_localize(None)
            sip_safe_end = (
                ny_now - timedelta(minutes=self._SIP_DELAY_MINUTES)
            ).to_pydatetime()
            end = min(end, sip_safe_end)

        request = StockBarsRequest(
            symbol_or_symbols=symbols,
            timeframe=timeframe,
            start=pd.Timestamp(start, tz="America/New_York"),
            end=pd.Timestamp(end, tz="America/New_York"),
            feed=self._daily_feed,
        )
        try:
            bars = self.stock_client.get_stock_bars(request)
        except Exception as exc:
            # Some accounts may not permit SIP at all -- degrade to IEX
            # rather than breaking the whole pipeline.
            if self._daily_feed == DataFeed.SIP and "subscription" in str(exc).lower():
                logger.warning(
                    "SIP historical data not permitted -- falling back to IEX "
                    "(volume/ADR will be understated): %s", exc
                )
                self._daily_feed = DataFeed.IEX
                request.feed = DataFeed.IEX
                bars = self.stock_client.get_stock_bars(request)
            else:
                raise
        return bars.df

    def get_intraday_bars(
        self,
        symbol: str,
        start: datetime,
        end: datetime,
        timeframe: TimeFrame = TimeFrame.Minute,
    ) -> pd.DataFrame:
        """Fetch intraday (1-min) bars for a single symbol within a window.

        Used to compute the Opening Range High/Low from the first
        ``orh_window_minutes`` after market open (typically 9:30–9:45).

        Parameters
        ----------
        symbol : str
            Ticker symbol.
        start : datetime
            Window start (e.g. 9:30 AM ET today).
        end : datetime
            Window end (e.g. 9:45 AM ET today).
        timeframe : TimeFrame
            Bar resolution (default 1-min).

        Returns
        -------
        pd.DataFrame
            OHLCV bars for the requested window.  Empty if no data.
        """
        request = StockBarsRequest(
            symbol_or_symbols=[symbol],
            timeframe=timeframe,
            start=pd.Timestamp(start, tz="America/New_York"),
            end=pd.Timestamp(end, tz="America/New_York"),
            feed=self._feed,
        )
        bars = self.stock_client.get_stock_bars(request)
        df = bars.df
        if df.empty:
            return df
        # Flatten multi-index if present
        if isinstance(df.index, pd.MultiIndex):
            try:
                df = df.xs(symbol, level="symbol")
            except KeyError:
                return pd.DataFrame()
        return df

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

    def compute_consolidation_pivot(
        self,
        symbol: str,
        bars: Optional[pd.DataFrame] = None,
        adr_period: int = 14,
        tight_ratio: float = 2 / 3,
        lookback_days: int = 100,
        recent_window: int = 20,
        min_tight_days: int = 2,
    ) -> Optional[dict]:
        """Detect consolidation resistance from tight daily bars.

        A "tight day" has a daily range (high - low) <= ``tight_ratio`` * ADR.
        This is the doc's definition: *"Daily Range <= 2/3 * ADR"*.

        Looks at the most recent ``recent_window`` trading days and finds
        all tight days.  Returns the highest high (entry trigger / resistance
        ceiling) and lowest low (stop level / consolidation floor).

        Parameters
        ----------
        symbol : str
            Ticker symbol.
        bars : pd.DataFrame, optional
            Pre-fetched daily bars to avoid redundant API calls.
            If ``None``, bars are fetched internally.
        adr_period : int
            Number of days for Average Daily Range calculation.
        tight_ratio : float
            Multiple of ADR that defines "tight" (default 2/3).
        lookback_days : int
            How far back to fetch bars (when *bars* is ``None``).
        recent_window : int
            Only consider tight days within this many recent trading days.
        min_tight_days : int
            Minimum number of tight days required.  Returns ``None``
            if fewer tight days are found (stock is not consolidating).

        Returns
        -------
        dict or None
            ``{"pivot_high", "pivot_low", "tight_days", "adr"}`` if a
            valid consolidation is detected, otherwise ``None``.
        """
        if bars is None:
            bars = self.get_bars(symbol, lookback_days=lookback_days)
        if bars.empty:
            return None

        if isinstance(bars.index, pd.MultiIndex):
            try:
                bars = bars.xs(symbol, level="symbol")
            except KeyError:
                return None

        if len(bars) < adr_period:
            return None

        # Compute ADR
        bars = bars.copy()
        bars["daily_range"] = bars["high"] - bars["low"]
        adr = float(bars["daily_range"].tail(adr_period).mean())
        if adr <= 0:
            return None

        tight_threshold = adr * tight_ratio

        # Find tight days in the recent window
        recent = bars.tail(recent_window)
        tight = recent[recent["daily_range"] <= tight_threshold]

        if len(tight) < min_tight_days:
            return None

        return {
            "pivot_high": round(float(tight["high"].max()), 2),
            "pivot_low": round(float(tight["low"].min()), 2),
            "tight_days": len(tight),
            "adr": round(adr, 2),
        }

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
