"""Pre-trade guardrails: safety validation before any order is submitted.

Every check here is a hard gate.  If ANY check fails, the order is blocked
and the reason is logged to the trade database.  The executor calls
``validate_entry()`` before every new position.

Checks:
- Max concurrent positions (default 6, reduced in elevated VIX)
- Max portfolio exposure (default 60%, reduced in elevated VIX)
- Max position size (10% of portfolio)
- Daily drawdown halt (-3% daily P&L = stop all new entries)
- Already-held filter (no duplicate positions)
- Regime pause (VIX > 30 = no new entries)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class GuardrailResult:
    """Result of running a trade proposal through guardrails."""

    approved: bool
    reason: str = ""
    checks: dict = None

    def __post_init__(self):
        if self.checks is None:
            self.checks = {}

    def __str__(self):
        status = "APPROVED" if self.approved else f"BLOCKED: {self.reason}"
        return f"Guardrail: {status}"


class Guardrails:
    """Pre-trade safety validation engine.

    Parameters
    ----------
    alpaca_client : AlpacaClient
        For querying account and positions.
    trade_db : TradeDB
        For checking existing positions and daily P&L.
    config : dict, optional
        Configuration dictionary with guardrails section.
    regime : dict, optional
        VIX regime adjustments from ``get_regime_adjustments()``.
        Overrides static config values when provided.
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

        self._client = alpaca_client
        self._db = trade_db
        cfg = config or DEFAULT_CONFIG
        gr = cfg.get("guardrails", {})
        sizing = get_sizing_params(cfg)

        # Static defaults from config
        self._max_position_pct = sizing["max_position_pct"]
        self._max_daily_loss_pct = gr.get("max_daily_loss_pct", 0.03)

        # Regime-adjusted values (VIX modulation overrides these)
        if regime:
            self._max_concurrent = regime.get("max_positions", sizing["max_concurrent_positions"])
            self._max_exposure_pct = regime.get("max_exposure_pct", sizing["max_exposure_pct"])
            self._pause_entries = regime.get("pause_entries", False)
        else:
            self._max_concurrent = sizing["max_concurrent_positions"]
            self._max_exposure_pct = sizing["max_exposure_pct"]
            self._pause_entries = False

    def validate_entry(
        self,
        symbol: str,
        proposed_value: float,
    ) -> GuardrailResult:
        """Validate whether a new entry for *symbol* should be allowed.

        Parameters
        ----------
        symbol : str
            Ticker symbol for the proposed trade.
        proposed_value : float
            Dollar value of the proposed position (qty × price).

        Returns
        -------
        GuardrailResult
            ``approved=True`` if all checks pass, otherwise ``approved=False``
            with the rejection reason.
        """
        checks = {}

        # 0. Regime pause (VIX > 30)
        if self._pause_entries:
            return GuardrailResult(
                approved=False,
                reason="VIX regime PANIC: all new entries paused",
                checks={"regime_pause": True},
            )
        checks["regime_pause"] = False

        # 1. Already held?
        # include_pending: an unfilled buy-stop entry still reserves a
        # position slot — otherwise N submitted stops could all trigger
        # and blow past max_concurrent_positions.
        if self._db:
            open_positions = self._db.get_open_positions(include_pending=True)
            held_symbols = {p["symbol"] for p in open_positions}

            if symbol in held_symbols:
                return GuardrailResult(
                    approved=False,
                    reason=f"already holding {symbol}",
                    checks={"already_held": True},
                )
            checks["already_held"] = False

            # 2. Max concurrent positions
            # Trimmed positions have near-zero risk (stop at/above breakeven)
            # so they shouldn't block new entries.  The dollar-exposure check
            # (3b) already accounts for their reduced market value.
            untrimmed = [p for p in open_positions if not p.get("trimmed", 0)]
            current_count = len(untrimmed)
            trimmed_count = len(open_positions) - current_count
            checks["positions_count"] = current_count
            checks["trimmed_positions"] = trimmed_count
            checks["positions_limit"] = self._max_concurrent
            if trimmed_count:
                logger.debug(
                    "Guardrail: %d trimmed position(s) excluded from count "
                    "(%d untrimmed / %d max)",
                    trimmed_count, current_count, self._max_concurrent,
                )
            if current_count >= self._max_concurrent:
                return GuardrailResult(
                    approved=False,
                    reason=f"max positions reached ({current_count}/{self._max_concurrent})",
                    checks=checks,
                )

        # 3. Portfolio-level checks (require Alpaca client)
        if self._client:
            try:
                portfolio_value = self._client.get_portfolio_value()
                checks["portfolio_value"] = portfolio_value

                # 3a. Max position size (10% of portfolio)
                max_position_value = portfolio_value * self._max_position_pct
                checks["proposed_value"] = proposed_value
                checks["max_position_value"] = max_position_value
                if proposed_value > max_position_value:
                    return GuardrailResult(
                        approved=False,
                        reason=(
                            f"position ${proposed_value:,.0f} exceeds "
                            f"{self._max_position_pct:.0%} cap (${max_position_value:,.0f})"
                        ),
                        checks=checks,
                    )

                # 3b. Max total exposure
                positions = self._client.get_all_positions()
                current_exposure = sum(
                    abs(float(p.market_value)) for p in positions
                )
                new_exposure = current_exposure + proposed_value
                max_exposure_value = portfolio_value * self._max_exposure_pct
                checks["current_exposure"] = current_exposure
                checks["new_exposure"] = new_exposure
                checks["max_exposure_value"] = max_exposure_value
                if new_exposure > max_exposure_value:
                    return GuardrailResult(
                        approved=False,
                        reason=(
                            f"total exposure ${new_exposure:,.0f} exceeds "
                            f"{self._max_exposure_pct:.0%} cap (${max_exposure_value:,.0f})"
                        ),
                        checks=checks,
                    )

            except Exception as exc:
                logger.error("Guardrail portfolio checks failed: %s", exc)
                return GuardrailResult(
                    approved=False,
                    reason=f"portfolio check error: {exc}",
                    checks=checks,
                )

        # 4. Daily drawdown halt
        if self._db and self._client:
            try:
                from datetime import date
                today = date.today().isoformat()
                snapshot = self._db.get_daily_snapshot(today)
                if snapshot and snapshot.get("daily_pnl_pct") is not None:
                    daily_pnl_pct = snapshot["daily_pnl_pct"]
                    checks["daily_pnl_pct"] = daily_pnl_pct
                    checks["drawdown_limit"] = -self._max_daily_loss_pct
                    if daily_pnl_pct <= -self._max_daily_loss_pct:
                        return GuardrailResult(
                            approved=False,
                            reason=(
                                f"daily drawdown halt: {daily_pnl_pct:.2%} "
                                f"exceeds -{self._max_daily_loss_pct:.0%} limit"
                            ),
                            checks=checks,
                        )
            except Exception as exc:
                logger.warning("Drawdown check failed: %s", exc)

        # All checks passed
        logger.info(
            "Guardrails APPROVED: %s ($%.0f)", symbol, proposed_value
        )
        return GuardrailResult(approved=True, checks=checks)
