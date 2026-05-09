"""Position Manager: deterministic exit logic for open positions.

This module runs DAILY after market close to evaluate every open position
against the doc's exit rules.  ALL logic is hard-coded — no LLM involvement.

Exit rules (from the doc):
1. Day 1 red close — if the stock closes below entry price on Day 1, exit
2. Day 3+ partial profit — sell 50% of remaining position on Day 3
3. Trailing 10 SMA — exit when price closes below the 10-day SMA
4. Max extension — exit if price is > 7× ADR above the 10 SMA (parabolic)
5. Soft backstop — flag (but don't auto-exit) positions held > 30 days

Stop management:
- Initial stop = ORL (Opening Range Low) from entry day
- After Day 1: replace stop with LOD (Low of Day) if higher
- After first green close: move stop to breakeven
- After Day 3: trailing 10 SMA stop
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class PositionAction:
    """An action to take on an open position."""

    symbol: str
    action: str  # "exit_full", "exit_partial", "update_stop", "hold", "flag"
    reason: str
    new_stop: Optional[float] = None
    exit_pct: float = 0.0  # fraction to sell (1.0 = full exit)


class PositionManager:
    """Evaluate open positions against the doc's exit rules.

    Parameters
    ----------
    data_client : AlpacaDataClient
        For fetching current bars and computing SMA/ADR.
    trade_db : TradeDB
        For reading position state and updating records.
    config : dict, optional
        Configuration dictionary.
    """

    def __init__(
        self,
        data_client=None,
        trade_db=None,
        config: Optional[Dict[str, Any]] = None,
    ):
        from tradingagents.strategies.swing_playbook import get_exit_rules

        self._data_client = data_client
        self._db = trade_db
        self._rules = get_exit_rules(config)

    @property
    def data_client(self):
        if self._data_client is None:
            from tradingagents.execution.alpaca_data import AlpacaDataClient
            self._data_client = AlpacaDataClient()
        return self._data_client

    def evaluate_all(self) -> List[PositionAction]:
        """Evaluate all open positions and return required actions.

        Returns
        -------
        list[PositionAction]
            One action per open position (may be "hold" if no action needed).
        """
        if not self._db:
            logger.warning("No trade DB configured — cannot evaluate positions")
            return []

        positions = self._db.get_open_positions()
        if not positions:
            logger.info("PositionManager: no open positions to evaluate")
            return []

        actions = []
        for pos in positions:
            action = self._evaluate_position(pos)
            actions.append(action)
            if action.action != "hold":
                logger.info(
                    "PositionManager: %s -> %s (%s)",
                    action.symbol, action.action, action.reason,
                )

        return actions

    def _evaluate_position(self, pos: Dict[str, Any]) -> PositionAction:
        """Evaluate a single position against exit rules.

        Rules are checked in priority order. First rule that fires wins.
        """
        symbol = pos["symbol"]
        entry_price = pos.get("entry_price", 0)
        current_stop = pos.get("stop_price", 0)
        day_count = pos.get("day_count", 1)
        trimmed = pos.get("trimmed", False)

        # Fetch current market data
        try:
            bars = self.data_client.get_bars(symbol, lookback_days=30)
            import pandas as pd
            if isinstance(bars.index, pd.MultiIndex):
                bars = bars.xs(symbol, level="symbol")

            if bars.empty:
                return PositionAction(symbol=symbol, action="hold", reason="no data")

            latest_close = float(bars["close"].iloc[-1])
            latest_low = float(bars["low"].iloc[-1])

        except Exception as exc:
            logger.warning("Failed to get data for %s: %s", symbol, exc)
            return PositionAction(symbol=symbol, action="hold", reason=f"data error: {exc}")

        # Rule 1: Day 1 red close
        if day_count == 1 and self._rules["day1_red_close_exit"]:
            if latest_close < entry_price:
                return PositionAction(
                    symbol=symbol,
                    action="exit_full",
                    reason=f"Day 1 red close: ${latest_close:.2f} < entry ${entry_price:.2f}",
                    exit_pct=1.0,
                )

        # Rule 2: Max extension (parabolic move)
        try:
            adr = self.data_client.compute_adr_pct(symbol, period=14) * latest_close
            sma_10 = self.data_client.compute_sma(symbol, period=10)
            if not sma_10.empty:
                sma_10_val = float(sma_10.iloc[-1])
                extension = (latest_close - sma_10_val) / adr if adr > 0 else 0
                if extension > self._rules["max_extension_adr_multiple"]:
                    return PositionAction(
                        symbol=symbol,
                        action="exit_full",
                        reason=(
                            f"Parabolic extension: {extension:.1f}x ADR above 10 SMA "
                            f"(limit: {self._rules['max_extension_adr_multiple']}x)"
                        ),
                        exit_pct=1.0,
                    )
        except Exception as exc:
            logger.warning("Extension check failed for %s: %s", symbol, exc)

        # Rule 3: Day 3+ partial profit (trim 50%)
        partial_day = self._rules["partial_profit_day"]
        if day_count >= partial_day and not trimmed:
            if latest_close > entry_price:  # only trim if profitable
                return PositionAction(
                    symbol=symbol,
                    action="exit_partial",
                    reason=f"Day {day_count} trim: selling {self._rules['partial_profit_pct']:.0%}",
                    exit_pct=self._rules["partial_profit_pct"],
                )

        # Rule 4: Trailing 10 SMA exit
        try:
            if not sma_10.empty:
                sma_10_val = float(sma_10.iloc[-1])
                trail_trigger = self._rules["trailing_ma_exit_on"]

                if trail_trigger == "close":
                    exit_triggered = latest_close < sma_10_val
                else:  # "low"
                    exit_triggered = latest_low < sma_10_val

                if day_count >= 3 and exit_triggered:
                    return PositionAction(
                        symbol=symbol,
                        action="exit_full",
                        reason=(
                            f"Trailing 10 SMA exit: {trail_trigger} ${latest_close:.2f} "
                            f"< 10 SMA ${sma_10_val:.2f}"
                        ),
                        exit_pct=1.0,
                    )

                # Update trailing stop to 10 SMA if higher than current stop
                if day_count >= 3 and sma_10_val > current_stop:
                    return PositionAction(
                        symbol=symbol,
                        action="update_stop",
                        reason=f"Trailing stop raised to 10 SMA ${sma_10_val:.2f}",
                        new_stop=sma_10_val,
                    )
        except Exception:
            pass  # sma_10 might not be computed yet, skip

        # Rule 5: Stop management (Day 1-2)
        if day_count <= 2:
            # Move stop to LOD if higher than current stop
            if latest_low > current_stop and latest_low < entry_price:
                return PositionAction(
                    symbol=symbol,
                    action="update_stop",
                    reason=f"Stop raised to LOD ${latest_low:.2f}",
                    new_stop=latest_low,
                )
            # Move to breakeven after first green close
            if latest_close > entry_price and current_stop < entry_price:
                return PositionAction(
                    symbol=symbol,
                    action="update_stop",
                    reason=f"Stop moved to breakeven ${entry_price:.2f}",
                    new_stop=entry_price,
                )

        # Rule 6: Soft backstop warning
        if day_count > self._rules["soft_backstop_days"]:
            return PositionAction(
                symbol=symbol,
                action="flag",
                reason=f"Position held {day_count} days (backstop: {self._rules['soft_backstop_days']})",
            )

        return PositionAction(symbol=symbol, action="hold", reason="all rules passed, holding")
