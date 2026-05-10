"""Executor: bridges LLM 'Buy' signals to real Alpaca bracket orders.

Flow:
1. Receive signal (symbol, entry_price, stop_price) from the pipeline
2. Calculate position size using risk-based formula
3. Run guardrails validation
4. Submit bracket order (entry + stop-loss) via AlpacaClient
5. Record the trade in TradeDB

Position sizing formula (from the doc):
    shares = (portfolio_value × risk_pct) / (entry_price - stop_price)
    position_value = shares × entry_price
    capped at max_position_pct of portfolio

All math is deterministic — no LLM involvement.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Any, Dict, Optional

from tradingagents.notifications import notify

logger = logging.getLogger(__name__)


@dataclass
class TradeSignal:
    """A validated trade signal from the LLM pipeline."""

    symbol: str
    action: str  # "buy", "sell", "hold"
    entry_price: float  # proposed entry (e.g. ORH level)
    stop_price: float  # initial stop (e.g. ORL or LOD)
    confidence: float = 0.0  # LLM confidence score (0-1)
    rationale: str = ""  # summary from the pipeline


@dataclass
class ExecutionResult:
    """Result of attempting to execute a trade."""

    success: bool
    symbol: str
    action: str
    shares: int = 0
    entry_price: float = 0.0
    stop_price: float = 0.0
    position_value: float = 0.0
    risk_amount: float = 0.0
    order_id: str = ""
    reason: str = ""  # rejection or error reason


class Executor:
    """Bridge between LLM signals and Alpaca order execution.

    Parameters
    ----------
    alpaca_client : AlpacaClient
        Execution client for submitting orders.
    trade_db : TradeDB
        Persistence layer for recording trades.
    config : dict, optional
        Configuration dictionary.
    regime : dict, optional
        VIX regime adjustments from ``check_market_regime()``.
    """

    def __init__(
        self,
        alpaca_client=None,
        trade_db=None,
        config: Optional[Dict[str, Any]] = None,
        regime: Optional[Dict[str, Any]] = None,
    ):
        from tradingagents.default_config import DEFAULT_CONFIG
        from tradingagents.strategies.swing_playbook import get_sizing_params
        from tradingagents.execution.guardrails import Guardrails

        self._client = alpaca_client
        self._db = trade_db
        cfg = config or DEFAULT_CONFIG
        sizing = get_sizing_params(cfg)

        # Regime-adjusted risk percentage (VIX modulation)
        if regime:
            self._risk_pct = regime.get("risk_pct", sizing["target_risk_pct"])
        else:
            self._risk_pct = sizing["target_risk_pct"]

        self._max_risk_pct = sizing["max_risk_pct"]
        self._max_position_pct = sizing["max_position_pct"]

        # Initialize guardrails with same regime
        self._guardrails = Guardrails(
            alpaca_client=alpaca_client,
            trade_db=trade_db,
            config=cfg,
            regime=regime,
        )

    def calculate_position_size(
        self,
        entry_price: float,
        stop_price: float,
        portfolio_value: float,
    ) -> Dict[str, Any]:
        """Calculate position size using risk-based formula.

        Formula:
            risk_per_share = entry_price - stop_price
            shares = (portfolio_value × risk_pct) / risk_per_share
            position_value = shares × entry_price
            capped at max_position_pct × portfolio_value

        Parameters
        ----------
        entry_price : float
            Proposed entry price.
        stop_price : float
            Initial stop-loss price.
        portfolio_value : float
            Total portfolio value.

        Returns
        -------
        dict
            ``shares``, ``position_value``, ``risk_amount``, ``risk_pct_actual``.
        """
        risk_per_share = entry_price - stop_price
        if risk_per_share <= 0:
            return {
                "shares": 0,
                "position_value": 0,
                "risk_amount": 0,
                "risk_pct_actual": 0,
                "error": "stop_price must be below entry_price",
            }

        # Risk-based sizing
        max_risk_dollars = portfolio_value * self._risk_pct
        shares = math.floor(max_risk_dollars / risk_per_share)

        if shares <= 0:
            return {
                "shares": 0,
                "position_value": 0,
                "risk_amount": 0,
                "risk_pct_actual": 0,
                "error": "risk too small for one share",
            }

        position_value = shares * entry_price
        risk_amount = shares * risk_per_share

        # Cap at max position size
        max_position_value = portfolio_value * self._max_position_pct
        if position_value > max_position_value:
            shares = math.floor(max_position_value / entry_price)
            position_value = shares * entry_price
            risk_amount = shares * risk_per_share

        # Cap risk at max_risk_pct
        if risk_amount > portfolio_value * self._max_risk_pct:
            shares = math.floor((portfolio_value * self._max_risk_pct) / risk_per_share)
            position_value = shares * entry_price
            risk_amount = shares * risk_per_share

        risk_pct_actual = risk_amount / portfolio_value if portfolio_value > 0 else 0

        return {
            "shares": shares,
            "position_value": round(position_value, 2),
            "risk_amount": round(risk_amount, 2),
            "risk_pct_actual": round(risk_pct_actual, 6),
        }

    def execute_entry(self, signal: TradeSignal) -> ExecutionResult:
        """Execute a buy signal: size → validate → submit bracket order.

        Parameters
        ----------
        signal : TradeSignal
            The trade signal from the LLM pipeline.

        Returns
        -------
        ExecutionResult
            Success/failure with details.
        """
        if signal.action != "buy":
            return ExecutionResult(
                success=False,
                symbol=signal.symbol,
                action=signal.action,
                reason=f"only 'buy' signals are executed (got '{signal.action}')",
            )

        if not self._client:
            return ExecutionResult(
                success=False,
                symbol=signal.symbol,
                action=signal.action,
                reason="no Alpaca client configured",
            )

        # 1. Get portfolio value
        try:
            portfolio_value = self._client.get_portfolio_value()
        except Exception as exc:
            return ExecutionResult(
                success=False,
                symbol=signal.symbol,
                action=signal.action,
                reason=f"failed to get portfolio value: {exc}",
            )

        # 2. Calculate position size
        sizing = self.calculate_position_size(
            signal.entry_price,
            signal.stop_price,
            portfolio_value,
        )
        if "error" in sizing:
            return ExecutionResult(
                success=False,
                symbol=signal.symbol,
                action=signal.action,
                reason=sizing["error"],
            )

        shares = sizing["shares"]
        position_value = sizing["position_value"]
        risk_amount = sizing["risk_amount"]

        logger.info(
            "Executor: %s -- %d shares @ $%.2f = $%.0f "
            "(risk $%.0f = %.2f%% of portfolio)",
            signal.symbol,
            shares,
            signal.entry_price,
            position_value,
            risk_amount,
            sizing["risk_pct_actual"] * 100,
        )

        # 3. Run guardrails
        guardrail = self._guardrails.validate_entry(signal.symbol, position_value)
        if not guardrail.approved:
            logger.warning(
                "Executor BLOCKED %s: %s", signal.symbol, guardrail.reason
            )
            notify("blocked", symbol=signal.symbol, reason=guardrail.reason)
            return ExecutionResult(
                success=False,
                symbol=signal.symbol,
                action=signal.action,
                shares=shares,
                entry_price=signal.entry_price,
                stop_price=signal.stop_price,
                position_value=position_value,
                risk_amount=risk_amount,
                reason=f"guardrail: {guardrail.reason}",
            )

        # 4. Submit bracket order (buy-stop at ORH)
        try:
            from alpaca.trading.enums import OrderSide

            # Buy-stop at ORH: order only fills if price breaks above ORH.
            # Stop-loss child at ORL protects downside.
            # Take-profit at entry + 3× risk as a generous ceiling.
            # Our PositionManager will typically exit before this via
            # trailing SMA, Day 3 trim, or parabolic extension rules.
            risk_per_share = signal.entry_price - signal.stop_price
            take_profit = round(signal.entry_price + 3 * risk_per_share, 2)

            order = self._client.submit_bracket_order(
                symbol=signal.symbol,
                qty=shares,
                side=OrderSide.BUY,
                entry_type="stop",
                stop_price=signal.entry_price,    # ORH = trigger price
                stop_loss_price=signal.stop_price, # ORL = stop-loss
                take_profit_price=take_profit,
            )
            order_id = str(order.id) if hasattr(order, "id") else str(order)
        except Exception as exc:
            logger.error("Executor: order submission failed for %s: %s", signal.symbol, exc)
            return ExecutionResult(
                success=False,
                symbol=signal.symbol,
                action=signal.action,
                shares=shares,
                entry_price=signal.entry_price,
                stop_price=signal.stop_price,
                position_value=position_value,
                risk_amount=risk_amount,
                reason=f"order submission failed: {exc}",
            )

        # 5. Record in trade DB
        if self._db:
            try:
                self._db.record_order(
                    symbol=signal.symbol,
                    side="buy",
                    qty=shares,
                    order_type="bracket",
                    limit_price=signal.entry_price,
                    stop_price=signal.stop_price,
                    order_id=order_id,
                    status="submitted",
                )
                self._db.open_position(
                    symbol=signal.symbol,
                    entry_date=__import__("datetime").date.today().isoformat(),
                    entry_price=signal.entry_price,
                    entry_orl=signal.stop_price,
                    qty=shares,
                    stop_order_id=order_id,
                )
            except Exception as exc:
                logger.warning("DB record failed (order still live): %s", exc)

        logger.info(
            "Executor: FILLED %s — %d shares @ $%.2f, stop @ $%.2f, order=%s",
            signal.symbol, shares, signal.entry_price, signal.stop_price, order_id,
        )

        notify(
            "entry",
            symbol=signal.symbol,
            shares=shares,
            entry=signal.entry_price,
            stop=signal.stop_price,
            value=position_value,
            risk=risk_amount,
            order_id=order_id,
        )

        return ExecutionResult(
            success=True,
            symbol=signal.symbol,
            action=signal.action,
            shares=shares,
            entry_price=signal.entry_price,
            stop_price=signal.stop_price,
            position_value=position_value,
            risk_amount=risk_amount,
            order_id=order_id,
        )
