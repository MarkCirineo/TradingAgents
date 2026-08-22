"""Pre-trade guardrails: safety validation before any order is submitted.

Every check here is a hard gate.  If ANY check fails, the order is blocked
and the reason is logged to the trade database.  The executor calls
``validate_entry()`` before every new position.

Checks:
- Max concurrent positions (default 10, reduced in elevated VIX) -- a
  backstop; portfolio heat is the primary concentration control
- Max portfolio heat (default 3.0% summed open risk, reduced in elevated VIX)
- Max sector exposure (default 30%, reduced in elevated VIX)
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
            self._max_heat_pct = regime.get("max_heat_pct", sizing["max_portfolio_heat_pct"])
            self._max_sector_pct = regime.get("max_sector_pct", sizing["max_sector_exposure_pct"])
            self._pause_entries = regime.get("pause_entries", False)
        else:
            self._max_concurrent = sizing["max_concurrent_positions"]
            self._max_exposure_pct = sizing["max_exposure_pct"]
            self._max_heat_pct = sizing["max_portfolio_heat_pct"]
            self._max_sector_pct = sizing["max_sector_exposure_pct"]
            self._pause_entries = False

    @staticmethod
    def _open_risk(open_positions: List[Dict[str, Any]]) -> float:
        """Return summed dollar risk across *open_positions*.

        Risk for a position is ``(entry_price - current_stop) × current_qty``,
        floored at zero: once a stop is raised to or above breakeven the
        position carries no downside risk to the account, so trimmed and
        breakeven-stopped positions naturally contribute ~0 and stop
        consuming heat.  PENDING entries count at their intended risk --
        an unfilled buy-stop still reserves risk the same way it reserves
        a slot.
        """
        total = 0.0
        for p in open_positions:
            entry = p.get("entry_price")
            stop = p.get("current_stop")
            qty = p.get("current_qty")
            if entry is None or stop is None or not qty:
                continue
            total += max(0.0, (float(entry) - float(stop)) * float(qty))
        return total

    def _check_sector(
        self,
        symbol: str,
        proposed_value: float,
        portfolio_value: float,
        open_positions: List[Dict[str, Any]],
        broker_positions: List[Any],
        checks: Dict[str, Any],
    ) -> Optional[GuardrailResult]:
        """Enforce the per-sector exposure cap.

        Returns a blocking ``GuardrailResult`` or ``None`` to continue.
        Any lookup failure returns ``None`` (fail open) -- see
        ``dataflows.sector``.  Unlike the exposure and heat checks, whose
        inputs are local, this one depends on a third-party data feed, so
        it must never be the reason trading halts.
        """
        try:
            return self._sector_result(
                symbol, proposed_value, portfolio_value,
                open_positions, broker_positions, checks,
            )
        except Exception as exc:
            logger.warning(
                "Sector cap check errored for %s (%s) -- not enforced",
                symbol, exc,
            )
            checks["sector_check_skipped"] = True
            return None

    def _sector_result(
        self,
        symbol: str,
        proposed_value: float,
        portfolio_value: float,
        open_positions: List[Dict[str, Any]],
        broker_positions: List[Any],
        checks: Dict[str, Any],
    ) -> Optional[GuardrailResult]:
        """Sector-cap arithmetic.  See ``_check_sector`` for the contract."""
        from tradingagents.dataflows.sector import get_sector, prime_cache

        # Reuse sectors already stored on position rows so a full book
        # costs at most one network lookup (the proposed symbol).
        prime_cache({
            p["symbol"]: p.get("sector")
            for p in open_positions if p.get("symbol")
        })

        proposed_sector = get_sector(symbol)
        checks["proposed_sector"] = proposed_sector
        if not proposed_sector:
            logger.warning(
                "Sector unavailable for %s -- sector cap not enforced", symbol
            )
            checks["sector_check_skipped"] = True
            return None

        # Market value comes from the broker (live prices); the DB tells us
        # which symbols belong to the sector.
        market_values = {
            p.symbol: abs(float(p.market_value)) for p in broker_positions
        }
        same_sector = 0.0
        for p in open_positions:
            sym = p.get("symbol")
            if not sym or sym == symbol:
                continue
            if get_sector(sym) != proposed_sector:
                continue
            # A PENDING entry has no broker position yet; fall back to its
            # intended notional so it still reserves sector room.
            same_sector += market_values.get(
                sym, float(p.get("entry_price") or 0) * float(p.get("current_qty") or 0)
            )

        new_sector_value = same_sector + proposed_value
        max_sector_value = portfolio_value * self._max_sector_pct
        checks["current_sector_exposure"] = round(same_sector, 2)
        checks["new_sector_exposure"] = round(new_sector_value, 2)
        checks["max_sector_value"] = round(max_sector_value, 2)
        if new_sector_value > max_sector_value:
            return GuardrailResult(
                approved=False,
                reason=(
                    f"{proposed_sector} exposure ${new_sector_value:,.0f} "
                    f"exceeds {self._max_sector_pct:.0%} sector cap "
                    f"(${max_sector_value:,.0f})"
                ),
                checks=checks,
            )
        return None

    def validate_entry(
        self,
        symbol: str,
        proposed_value: float,
        proposed_risk: Optional[float] = None,
    ) -> GuardrailResult:
        """Validate whether a new entry for *symbol* should be allowed.

        Parameters
        ----------
        symbol : str
            Ticker symbol for the proposed trade.
        proposed_value : float
            Dollar value of the proposed position (qty × price).
        proposed_risk : float, optional
            Dollar risk of the proposed position -- ``(entry - stop) × qty``.
            When omitted the portfolio-heat check is skipped, so callers
            that size by risk should always pass it.

        Returns
        -------
        GuardrailResult
            ``approved=True`` if all checks pass, otherwise ``approved=False``
            with the rejection reason.
        """
        checks = {}
        open_positions: List[Dict[str, Any]] = []

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

                # 3c. Max portfolio heat -- summed open risk.
                # This is the primary concentration control: unlike a raw
                # position count it scales with how much each trade actually
                # risks, so tight setups earn more slots than sloppy ones.
                if proposed_risk is not None:
                    current_heat = self._open_risk(open_positions)
                    new_heat = current_heat + proposed_risk
                    max_heat_value = portfolio_value * self._max_heat_pct
                    checks["current_heat"] = round(current_heat, 2)
                    checks["new_heat"] = round(new_heat, 2)
                    checks["max_heat_value"] = round(max_heat_value, 2)
                    checks["new_heat_pct"] = (
                        round(new_heat / portfolio_value, 6)
                        if portfolio_value > 0 else 0
                    )
                    if new_heat > max_heat_value:
                        return GuardrailResult(
                            approved=False,
                            reason=(
                                f"portfolio heat ${new_heat:,.0f} exceeds "
                                f"{self._max_heat_pct:.2%} cap "
                                f"(${max_heat_value:,.0f})"
                            ),
                            checks=checks,
                        )

                # 3d. Max sector exposure -- momentum leaders cluster into
                # themes, so N positions can amount to a single bet.
                # Fails open: an unresolvable sector skips the check rather
                # than halting trading on a data-provider outage.
                sector_result = self._check_sector(
                    symbol, proposed_value, portfolio_value,
                    open_positions, positions, checks,
                )
                if sector_result is not None:
                    return sector_result

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
