"""Alpaca Trading API wrapper for paper (and eventually live) trading.

Thin abstraction over ``alpaca-py``'s ``TradingClient``.  All Alpaca
interactions go through this module so the rest of the codebase never
imports ``alpaca`` directly.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Optional

from alpaca.common.exceptions import APIError
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import (
    OrderClass,
    OrderSide,
    OrderStatus,
    OrderType,
    QueryOrderStatus,
    TimeInForce,
)
from alpaca.trading.requests import (
    ClosePositionRequest,
    GetCalendarRequest,
    GetOrdersRequest,
    LimitOrderRequest,
    MarketOrderRequest,
    StopLossRequest,
    StopOrderRequest,
    TakeProfitRequest,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Custom exceptions
# ---------------------------------------------------------------------------

class AlpacaConnectionError(Exception):
    """Raised when the Alpaca API is unreachable or credentials are invalid."""


class AlpacaOrderError(Exception):
    """Raised when an order submission or modification fails."""


# ---------------------------------------------------------------------------
# Client wrapper
# ---------------------------------------------------------------------------

class AlpacaClient:
    """Wrapper around ``alpaca-py``'s ``TradingClient``.

    Parameters
    ----------
    api_key : str, optional
        Alpaca API key.  Falls back to ``ALPACA_API_KEY`` env var.
    secret_key : str, optional
        Alpaca secret key.  Falls back to ``ALPACA_SECRET_KEY`` env var.
    paper : bool
        If ``True`` (default), connect to the paper-trading environment.
    max_retries : int
        Number of retries on transient API errors.
    retry_base_delay : float
        Base delay in seconds for exponential backoff between retries.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        secret_key: Optional[str] = None,
        paper: bool = True,
        max_retries: int = 3,
        retry_base_delay: float = 1.0,
    ):
        self._api_key = api_key or os.environ.get("ALPACA_API_KEY", "")
        self._secret_key = secret_key or os.environ.get("ALPACA_SECRET_KEY", "")
        if not self._api_key or not self._secret_key:
            raise AlpacaConnectionError(
                "Alpaca API credentials not found. Set ALPACA_API_KEY and "
                "ALPACA_SECRET_KEY environment variables or pass them directly."
            )
        self._paper = paper
        self._max_retries = max_retries
        self._retry_base_delay = retry_base_delay
        self._client: Optional[TradingClient] = None

    # -- lazy initialization ------------------------------------------------

    @property
    def client(self) -> TradingClient:
        """Lazily initialize the underlying ``TradingClient``."""
        if self._client is None:
            try:
                self._client = TradingClient(
                    self._api_key,
                    self._secret_key,
                    paper=self._paper,
                )
                logger.info(
                    "Alpaca TradingClient initialised (paper=%s)", self._paper
                )
            except Exception as exc:
                raise AlpacaConnectionError(
                    f"Failed to initialise Alpaca TradingClient: {exc}"
                ) from exc
        return self._client

    # -- retry helper -------------------------------------------------------

    def _retry(self, fn, *args, **kwargs):
        """Call *fn* with exponential-backoff retries on transient errors."""
        last_exc = None
        for attempt in range(1, self._max_retries + 1):
            try:
                return fn(*args, **kwargs)
            except APIError as exc:
                last_exc = exc
                # 429 (rate limit) and 5xx are transient
                status = getattr(exc, "status_code", None)
                if status and (status == 429 or status >= 500):
                    delay = self._retry_base_delay * (2 ** (attempt - 1))
                    logger.warning(
                        "Alpaca API error (attempt %d/%d, status=%s): %s  "
                        "-- retrying in %.1fs",
                        attempt,
                        self._max_retries,
                        status,
                        exc,
                        delay,
                    )
                    time.sleep(delay)
                else:
                    raise
            except Exception as exc:
                raise AlpacaOrderError(
                    f"Unexpected error calling Alpaca API: {exc}"
                ) from exc
        raise AlpacaOrderError(
            f"Alpaca API call failed after {self._max_retries} retries: "
            f"{last_exc}"
        ) from last_exc

    # -- account ------------------------------------------------------------

    def get_account(self):
        """Return the current Alpaca account object.

        Key fields: ``cash``, ``portfolio_value``, ``equity``,
        ``buying_power``, ``status``.
        """
        return self._retry(self.client.get_account)

    # -- market clock & calendar --------------------------------------------

    def get_clock(self):
        """Return the market clock (``is_open``, ``next_open``, ``next_close``)."""
        return self._retry(self.client.get_clock)

    def get_calendar(self, start=None, end=None):
        """Return the trading calendar between *start* and *end*."""
        filters = GetCalendarRequest(start=start, end=end)
        return self._retry(self.client.get_calendar, filters=filters)

    # -- positions ----------------------------------------------------------

    def get_all_positions(self):
        """Return a list of all open positions."""
        return self._retry(self.client.get_all_positions)

    def get_position(self, symbol: str):
        """Return the open position for *symbol*, or ``None``."""
        try:
            return self._retry(self.client.get_open_position, symbol)
        except APIError:
            return None

    def close_position(self, symbol: str, qty: Optional[float] = None):
        """Close (fully or partially) the position for *symbol*.

        Parameters
        ----------
        symbol : str
            Ticker symbol.
        qty : float, optional
            Number of shares to sell.  If ``None``, closes the full position.
        """
        request = ClosePositionRequest(qty=str(qty)) if qty else None
        return self._retry(
            self.client.close_position, symbol, close_options=request
        )

    # -- orders: submission -------------------------------------------------

    def submit_market_order(
        self,
        symbol: str,
        qty: float,
        side: OrderSide,
        time_in_force: TimeInForce = TimeInForce.DAY,
    ):
        """Submit a simple market order."""
        request = MarketOrderRequest(
            symbol=symbol,
            qty=qty,
            side=side,
            time_in_force=time_in_force,
        )
        return self._retry(self.client.submit_order, order_data=request)

    def submit_stop_order(
        self,
        symbol: str,
        qty: float,
        side: OrderSide,
        stop_price: float,
        time_in_force: TimeInForce = TimeInForce.GTC,
    ):
        """Submit a stop order (e.g. a stop-loss sell)."""
        request = StopOrderRequest(
            symbol=symbol,
            qty=qty,
            side=side,
            stop_price=stop_price,
            time_in_force=time_in_force,
        )
        return self._retry(self.client.submit_order, order_data=request)

    def submit_bracket_order(
        self,
        symbol: str,
        qty: float,
        side: OrderSide,
        entry_type: str = "market",
        limit_price: Optional[float] = None,
        stop_price: Optional[float] = None,
        stop_loss_price: Optional[float] = None,
        take_profit_price: Optional[float] = None,
        time_in_force: TimeInForce = TimeInForce.GTC,
    ):
        """Submit a bracket order with attached stop-loss (and optional take-profit).

        This is used for ORH entries: the parent is a buy-stop order at the
        ORH price, and the stop-loss child is set at the ORL price.

        Parameters
        ----------
        entry_type : str
            ``"market"``, ``"limit"``, or ``"stop"``.
        limit_price : float, optional
            Required when *entry_type* is ``"limit"``.
        stop_price : float, optional
            Required when *entry_type* is ``"stop"`` (e.g. buy-stop at ORH).
        stop_loss_price : float, optional
            Price for the attached stop-loss child order (e.g. ORL).
        take_profit_price : float, optional
            Price for the attached take-profit child order.
        """
        kwargs = dict(
            symbol=symbol,
            qty=qty,
            side=side,
            time_in_force=time_in_force,
            order_class=OrderClass.BRACKET,
        )

        if stop_loss_price:
            kwargs["stop_loss"] = StopLossRequest(stop_price=stop_loss_price)
        if take_profit_price:
            kwargs["take_profit"] = TakeProfitRequest(
                limit_price=take_profit_price
            )

        if entry_type == "stop":
            if stop_price is None:
                raise ValueError("stop_price is required for stop entries")
            kwargs["type"] = OrderType.STOP
            kwargs["stop_price"] = stop_price
            request = StopOrderRequest(**kwargs)
        elif entry_type == "limit":
            if limit_price is None:
                raise ValueError("limit_price is required for limit entries")
            kwargs["type"] = OrderType.LIMIT
            kwargs["limit_price"] = limit_price
            request = LimitOrderRequest(**kwargs)
        elif entry_type == "market":
            request = MarketOrderRequest(**kwargs)
        else:
            raise ValueError(f"Unknown entry_type '{entry_type}' — expected 'market', 'limit', or 'stop'")

        return self._retry(self.client.submit_order, order_data=request)

    # -- orders: query & cancel ---------------------------------------------

    def get_orders(
        self,
        status: QueryOrderStatus = QueryOrderStatus.OPEN,
        symbols: Optional[list[str]] = None,
    ):
        """Return orders matching the given *status* and optional *symbols*."""
        request = GetOrdersRequest(status=status, symbols=symbols)
        return self._retry(self.client.get_orders, filter=request)

    def get_order_by_id(self, order_id: str):
        """Return a single order by its Alpaca order ID."""
        return self._retry(self.client.get_order_by_id, order_id)

    def cancel_order(self, order_id: str):
        """Cancel a single order by ID."""
        return self._retry(self.client.cancel_order_by_id, order_id)

    def cancel_all_orders(self):
        """Cancel all open orders."""
        return self._retry(self.client.cancel_orders)

    # -- orders: nested (bracket leg visibility) ----------------------------

    def get_orders_nested(
        self,
        status: QueryOrderStatus = QueryOrderStatus.ALL,
        symbols: Optional[list[str]] = None,
    ):
        """Return orders with bracket child legs expanded.

        The ``nested=True`` parameter tells Alpaca to include the
        ``legs`` list on bracket orders, containing the stop-loss
        and take-profit child orders with their prices.  This is
        the data that Alpaca's own dashboard UI *doesn't* show.

        For bracket orders, ``order.legs`` is a list where:
        - ``legs[0]`` → take-profit order (has ``limit_price``)
        - ``legs[1]`` → stop-loss order (has ``stop_price``)
        """
        request = GetOrdersRequest(status=status, symbols=symbols, nested=True)
        return self._retry(self.client.get_orders, filter=request)

    def get_order_nested(self, order_id: str):
        """Return a single order with bracket legs expanded."""
        return self._retry(
            self.client.get_order_by_id, order_id, nested=True
        )

    # -- orders: modification -----------------------------------------------

    def replace_stop_order(
        self,
        order_id: str,
        new_stop_price: float,
        qty: Optional[float] = None,
    ):
        """Replace (modify) an existing stop order's price.

        Used to update stops as the trade progresses:
        ORL -> LOD -> breakeven -> 10 SMA trailing.
        """
        from alpaca.trading.requests import ReplaceOrderRequest

        kwargs = {"stop_price": new_stop_price}
        if qty is not None:
            kwargs["qty"] = qty
        request = ReplaceOrderRequest(**kwargs)
        return self._retry(
            self.client.replace_order_by_id, order_id, request
        )

    # -- convenience --------------------------------------------------------

    def is_market_open(self) -> bool:
        """Return ``True`` if the market is currently open."""
        clock = self.get_clock()
        return clock.is_open

    def get_portfolio_value(self) -> float:
        """Return the current portfolio value as a float."""
        account = self.get_account()
        return float(account.portfolio_value)

    def get_cash(self) -> float:
        """Return available cash as a float."""
        account = self.get_account()
        return float(account.cash)
