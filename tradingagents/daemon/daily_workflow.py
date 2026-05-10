"""Daily workflow: the actual trading logic executed by the scheduler.

Each function maps to a time slot in the trading day:

    8:00 AM  pre_market()     — screener + regime check + pre-filter
    8:05 AM  analyze()        — LLM/quant pipeline → store decisions
    9:45 AM  entry_window()   — fetch ORH/ORL → submit buy-stop orders
   12:00 PM  midday_check()   — Day 3 trims, parabolic extension exits
    3:45 PM  eod_check()      — Day 1 red close, trailing SMA, stop updates
    4:15 PM  post_market()    — daily snapshot, increment day counts, log summary

All functions receive a shared ``DayContext`` that carries state across
the trading day (regime, candidates, etc.).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional

from tradingagents.notifications import notify

logger = logging.getLogger(__name__)


@dataclass
class DayContext:
    """Shared state for a single trading day.

    Created by ``pre_market()`` and consumed by later steps.
    """

    date: str = ""
    regime: Dict[str, Any] = field(default_factory=dict)
    regime_favorable: bool = False
    candidates: List[str] = field(default_factory=list)
    pipeline_decisions: Dict[str, str] = field(default_factory=dict)
    entries_submitted: List[Dict[str, Any]] = field(default_factory=list)
    exits_executed: List[Dict[str, Any]] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)


class DailyWorkflow:
    """Orchestrates the full trading day.

    Parameters
    ----------
    config : dict, optional
        Full configuration dictionary.
    """

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        from tradingagents.default_config import DEFAULT_CONFIG

        self._config = config or DEFAULT_CONFIG
        self._ctx: Optional[DayContext] = None

        # Lazy-init components (only when actually running)
        self._data_client = None
        self._alpaca_client = None
        self._trade_db = None

    def _ensure_components(self):
        """Lazily initialize all shared components."""
        if self._data_client is None:
            from tradingagents.execution.alpaca_data import AlpacaDataClient
            self._data_client = AlpacaDataClient()

        if self._alpaca_client is None:
            from tradingagents.execution.alpaca_client import AlpacaClient
            self._alpaca_client = AlpacaClient()

        if self._trade_db is None:
            from tradingagents.execution.trade_db import TradeDB
            self._trade_db = TradeDB()

    # -- 8:00 AM: Pre-market ------------------------------------------------

    def pre_market(self) -> DayContext:
        """Screen tickers and check market regime.

        This creates the ``DayContext`` for the day. If the regime is
        unfavorable, ``candidates`` will be empty and no entries will
        be attempted.
        """
        self._ensure_components()
        self._ctx = DayContext(date=date.today().isoformat())

        logger.info("=== PRE-MARKET %s ===", self._ctx.date)

        # 1. Market regime check (SPY MAs + VIX regime)
        from tradingagents.screening.pre_filter import check_market_regime

        regime_result = check_market_regime(
            data_client=self._data_client,
            config=self._config,
        )
        self._ctx.regime_favorable = regime_result["favorable"]
        self._ctx.regime = regime_result.get("regime", {})

        regime_label = self._ctx.regime.get("label", "Unknown")
        logger.info(
            "Market regime: %s (favorable=%s)",
            regime_label, self._ctx.regime_favorable,
        )

        if not self._ctx.regime_favorable:
            logger.warning("Regime UNFAVORABLE — no new entries today")
            return self._ctx

        # 2. Screen tickers
        from tradingagents.screening.screener import HybridScreener

        screener = HybridScreener(
            data_client=self._data_client,
            config=self._config,
        )
        candidates = screener.scan()

        # 3. Pre-filter
        from tradingagents.screening.pre_filter import PreFilter

        pf = PreFilter(
            data_client=self._data_client,
            trade_db=self._trade_db,
            config=self._config,
        )
        symbols = [c.symbol for c in candidates]
        filtered = pf.filter_candidates(symbols)

        self._ctx.candidates = [r.symbol for r in filtered]
        logger.info(
            "Pre-market: %d screened -> %d passed pre-filter: %s",
            len(candidates),
            len(filtered),
            self._ctx.candidates,
        )

        # Log to trade DB
        try:
            self._trade_db.log_screening(
                date=self._ctx.date,
                screened=len(candidates),
                passed=len(filtered),
                symbols=self._ctx.candidates,
                regime=regime_label,
            )
        except Exception as exc:
            logger.warning("Failed to log screening: %s", exc)

        return self._ctx

    # -- 8:05 AM: Analyze (LLM pipeline) ------------------------------------

    def analyze(self) -> DayContext:
        """Run the LLM/quant pipeline on candidates and store decisions.

        This runs BEFORE market open to give the LLM plenty of time.
        Decisions are stored in ``self._ctx.pipeline_decisions`` and
        consumed by ``entry_window()`` at 9:45 AM.
        """
        if self._ctx is None:
            logger.error("analyze called before pre_market")
            return DayContext()

        if not self._ctx.regime_favorable:
            logger.info("Skipping analysis — regime unfavorable")
            return self._ctx

        if not self._ctx.candidates:
            logger.info("Skipping analysis — no candidates")
            return self._ctx

        self._ensure_components()

        candidates = self._ctx.candidates
        pipeline_mode = self._config.get("pipeline_mode", "full")

        logger.info(
            "=== ANALYZE %s (mode=%s, %d candidates) ===",
            self._ctx.date, pipeline_mode, len(candidates),
        )

        if pipeline_mode == "quant":
            pipeline_results = self._quant_decisions(candidates)
        else:
            pipeline_results = self._llm_pipeline_batch(candidates)

        # Store decisions for entry_window to consume
        for symbol in candidates:
            decision = pipeline_results.get(symbol)
            if decision is not None:
                action = self._parse_decision(decision)
                self._ctx.pipeline_decisions[symbol] = action
                # Update screening log
                self._trade_db.log_screening_result(
                    date=self._ctx.date, symbol=symbol,
                    source="hybrid_screener", score=1.0,
                    selected_for_pipeline=True, signal_result=action,
                )
                logger.info("%s: pipeline decision = %s", symbol, action)

        buy_count = sum(1 for v in self._ctx.pipeline_decisions.values() if v == "buy")
        logger.info(
            "Analysis complete: %d/%d candidates are BUY",
            buy_count, len(candidates),
        )
        return self._ctx

    # -- 9:45 AM: Entry window (ORH/ORL execution) --------------------------

    def entry_window(self) -> DayContext:
        """Execute entries using Opening Range breakout logic.

        Reads pre-computed decisions from ``analyze()`` and for each
        "buy" signal:
          1. Fetches 1-min intraday bars for the opening range window
          2. Computes ORH (Opening Range High) and ORL (Opening Range Low)
          3. Submits a buy-stop order at ORH with stop-loss at ORL

        The buy-stop only fills if price breaks above ORH during the day.
        If ORH is never breached, the order expires at market close.
        """
        if self._ctx is None:
            logger.error("entry_window called before pre_market")
            return DayContext()

        if not self._ctx.regime_favorable:
            logger.info("Skipping entries — regime unfavorable")
            return self._ctx

        if not self._ctx.pipeline_decisions:
            logger.info("Skipping entries — no pipeline decisions (analyze() not run?)")
            return self._ctx

        self._ensure_components()

        from tradingagents.execution.executor import Executor, TradeSignal

        executor = Executor(
            alpaca_client=self._alpaca_client,
            trade_db=self._trade_db,
            config=self._config,
            regime=self._ctx.regime,
        )

        buy_symbols = [
            s for s, d in self._ctx.pipeline_decisions.items() if d == "buy"
        ]
        logger.info(
            "Entry window: %d BUY decisions to execute via ORH breakout",
            len(buy_symbols),
        )

        for symbol in buy_symbols:
            try:
                # Calculate ORH/ORL from today's opening range
                orh, orl = self._calculate_orh_orl(symbol)
                if orh <= 0 or orl <= 0:
                    logger.warning("%s: invalid ORH/ORL — skipping", symbol)
                    self._trade_db.log_screening_result(
                        date=self._ctx.date, symbol=symbol,
                        source="hybrid_screener", score=1.0,
                        selected_for_pipeline=True, signal_result="error:orh_orl",
                    )
                    continue

                if orl >= orh:
                    logger.warning(
                        "%s: ORL ($%.2f) >= ORH ($%.2f) — flat opening range, skipping",
                        symbol, orl, orh,
                    )
                    self._trade_db.log_screening_result(
                        date=self._ctx.date, symbol=symbol,
                        source="hybrid_screener", score=1.0,
                        selected_for_pipeline=True, signal_result="skip:flat_range",
                    )
                    continue

                logger.info(
                    "%s: ORH=$%.2f, ORL=$%.2f — submitting buy-stop",
                    symbol, orh, orl,
                )

                signal = TradeSignal(
                    symbol=symbol,
                    action="buy",
                    entry_price=orh,   # buy-stop trigger at ORH
                    stop_price=orl,    # stop-loss at ORL
                    rationale=f"ORH breakout: buy-stop @ ${orh:.2f}, stop @ ${orl:.2f}",
                )

                result = executor.execute_entry(signal)
                if result.success:
                    self._ctx.entries_submitted.append({
                        "symbol": symbol,
                        "shares": result.shares,
                        "entry": result.entry_price,
                        "stop": result.stop_price,
                        "value": result.position_value,
                    })
                    logger.info(
                        "ENTRY: %s — %d shares, buy-stop @ $%.2f, stop @ $%.2f",
                        symbol, result.shares, result.entry_price, result.stop_price,
                    )
                    self._trade_db.log_screening_result(
                        date=self._ctx.date, symbol=symbol,
                        source="hybrid_screener", score=1.0,
                        selected_for_pipeline=True, signal_result="buy:submitted",
                    )
                else:
                    logger.info("%s: entry blocked — %s", symbol, result.reason)
                    self._trade_db.log_screening_result(
                        date=self._ctx.date, symbol=symbol,
                        source="hybrid_screener", score=1.0,
                        selected_for_pipeline=True, signal_result=f"blocked:{result.reason[:50]}",
                    )

            except Exception as exc:
                error_msg = f"Execution error for {symbol}: {exc}"
                logger.error(error_msg)
                self._ctx.errors.append(error_msg)

        logger.info(
            "Entry window complete: %d entries submitted",
            len(self._ctx.entries_submitted),
        )
        return self._ctx

    def _quant_decisions(self, candidates: List[str]) -> Dict[str, Optional[str]]:
        """Generate synthetic 'buy' decisions for all candidates (quant mode).

        In quant mode, passing the pre-filter IS the entry signal.
        No LLM is invoked. The decision string records the mode for audit.
        """
        logger.info(
            "QUANT MODE: auto-buy for %d candidates (no LLM)", len(candidates),
        )
        results = {}
        for symbol in candidates:
            results[symbol] = (
                f"**Rating: Buy**\n"
                f"QUANT MODE — automatic entry. Passed all quantitative "
                f"pre-filter criteria (dollar volume, ADR, price range, "
                f"relative strength, market regime).\n"
                f"Pipeline mode: quant (no LLM analysis performed)\n"
            )
            logger.info("%s: QUANT auto-buy (pre-filter passed)", symbol)
        return results

    def _llm_pipeline_batch(self, candidates: List[str]) -> Dict[str, Optional[str]]:
        """Run LLM pipeline concurrently for all candidates (full mode).

        Uses ThreadPoolExecutor for ~2x throughput. Each ticker gets its
        own TradingAgentsGraph instance for thread safety.
        """
        from concurrent.futures import ThreadPoolExecutor, as_completed

        max_workers = self._config.get("screening", {}).get("max_workers", 2)
        pipeline_results = {}

        logger.info(
            "FULL MODE: running LLM pipeline for %d candidates (max_workers=%d)...",
            len(candidates), max_workers,
        )

        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {
                pool.submit(self._run_analysis_pipeline, symbol): symbol
                for symbol in candidates
            }
            for future in as_completed(futures):
                symbol = futures[future]
                try:
                    decision = future.result()
                    pipeline_results[symbol] = decision
                    if decision is None:
                        error_msg = f"{symbol}: LLM pipeline returned no decision"
                        logger.warning(error_msg)
                        self._ctx.errors.append(error_msg)
                except Exception as exc:
                    error_msg = f"Pipeline error for {symbol}: {exc}"
                    logger.error(error_msg)
                    self._ctx.errors.append(error_msg)

        logger.info(
            "Pipeline batch complete: %d/%d succeeded",
            sum(1 for v in pipeline_results.values() if v is not None),
            len(candidates),
        )
        return pipeline_results

    # -- 12:00 PM: Midday check ---------------------------------------------

    def midday_check(self) -> DayContext:
        """Run midday position management: Day 3 trims, parabolic exits."""
        if self._ctx is None:
            self._ctx = DayContext(date=date.today().isoformat())

        self._ensure_components()

        from tradingagents.execution.position_manager import PositionManager

        pm = PositionManager(
            data_client=self._data_client,
            trade_db=self._trade_db,
            config=self._config,
        )

        actions = pm.evaluate_all()
        for action in actions:
            if action.action in ("exit_partial", "exit_full") and "arabolic" in action.reason:
                # Parabolic exits happen at midday
                self._execute_exit(action)
            elif action.action == "exit_partial" and "trim" in action.reason.lower():
                # Day 3 trims happen at midday
                self._execute_exit(action)

        return self._ctx

    # -- 3:45 PM: EOD check -------------------------------------------------

    def eod_check(self) -> DayContext:
        """Run EOD position management: Day 1 red close, trailing SMA, stops."""
        if self._ctx is None:
            self._ctx = DayContext(date=date.today().isoformat())

        self._ensure_components()

        from tradingagents.execution.position_manager import PositionManager

        pm = PositionManager(
            data_client=self._data_client,
            trade_db=self._trade_db,
            config=self._config,
        )

        actions = pm.evaluate_all()
        for action in actions:
            if action.action == "exit_full" and "Day 1" in action.reason:
                self._execute_exit(action)
            elif action.action == "exit_full" and "SMA" in action.reason:
                self._execute_exit(action)
            elif action.action == "update_stop":
                self._execute_stop_update(action)
            elif action.action == "flag":
                logger.warning("FLAGGED: %s — %s", action.symbol, action.reason)

        return self._ctx

    # -- 4:15 PM: Post-market summary ---------------------------------------

    def post_market(self) -> DayContext:
        """Record daily snapshot and increment position day counts."""
        if self._ctx is None:
            self._ctx = DayContext(date=date.today().isoformat())

        self._ensure_components()

        # Increment day counts for all open positions
        try:
            positions = self._trade_db.get_open_positions()
            for pos in positions:
                self._trade_db.increment_day_count(pos["symbol"])
        except Exception as exc:
            logger.warning("Failed to increment day counts: %s", exc)

        # Record daily portfolio snapshot
        try:
            portfolio_value = self._alpaca_client.get_portfolio_value()
            cash = self._alpaca_client.get_cash()
            positions = self._alpaca_client.get_all_positions()
            num_positions = len(positions)

            self._trade_db.record_snapshot(
                date=self._ctx.date,
                portfolio_value=portfolio_value,
                cash=cash,
                num_positions=num_positions,
                entries_today=len(self._ctx.entries_submitted),
                exits_today=len(self._ctx.exits_executed),
                regime=self._ctx.regime.get("label", "Unknown"),
            )
            logger.info(
                "Daily snapshot: portfolio=$%.0f, cash=$%.0f, "
                "positions=%d, entries=%d, exits=%d",
                portfolio_value, cash, num_positions,
                len(self._ctx.entries_submitted),
                len(self._ctx.exits_executed),
            )

            # Send daily summary notification
            notify(
                "daily_summary",
                portfolio=portfolio_value,
                entries=len(self._ctx.entries_submitted),
                exits=len(self._ctx.exits_executed),
                positions=num_positions,
                pnl=0,  # TODO: calculate actual P&L from snapshots
            )
        except Exception as exc:
            logger.warning("Failed to record snapshot: %s", exc)

        if self._ctx.errors:
            logger.warning("Day had %d errors: %s", len(self._ctx.errors), self._ctx.errors)
            notify("error", message=f"Day had {len(self._ctx.errors)} errors: {', '.join(self._ctx.errors[:3])}")

        logger.info("=== POST-MARKET %s COMPLETE ===", self._ctx.date)
        return self._ctx

    # -- helpers ------------------------------------------------------------

    def _run_analysis_pipeline(self, symbol: str) -> Optional[str]:
        """Run the TradingAgentsGraph for a single ticker.

        Uses graph streaming to log each agent node as it completes,
        giving visibility into which step is running.

        Returns the final_trade_decision string or None.
        """
        try:
            import time
            from tradingagents.graph.trading_graph import TradingAgentsGraph

            start = time.time()

            # Use daemon config so the strategy overlay is injected
            daemon_config = {**self._config, "trading_mode": "daemon"}
            graph = TradingAgentsGraph(config=daemon_config)

            # propagate() returns (final_state_dict, processed_signal)
            final_state, processed_signal = graph.propagate(symbol, self._ctx.date)
            decision = final_state.get("final_trade_decision")

            elapsed = time.time() - start
            logger.info(
                "Pipeline COMPLETE for %s in %.0fs: signal=%s",
                symbol, elapsed, processed_signal,
            )
            return decision
        except Exception as exc:
            logger.error("Analysis pipeline failed for %s: %s", symbol, exc)
            return None

    def _parse_decision(self, decision: str) -> str:
        """Extract action from portfolio manager's decision text.

        Returns "buy", "sell", or "hold".
        """
        if not decision:
            return "hold"
        text = decision.lower()
        # Look for the rating keywords
        if "**buy**" in text or "rating: buy" in text or "recommendation: buy" in text:
            return "buy"
        if "**sell**" in text or "rating: sell" in text:
            return "sell"
        return "hold"

    def _calculate_orh_orl(self, symbol: str) -> tuple:
        """Calculate Opening Range High and Low from intraday bars.

        Fetches 1-min bars for the first ``orh_window_minutes`` after
        market open (default: 9:30–9:45 ET).

        Returns
        -------
        tuple
            (ORH, ORL) — highest high and lowest low in the opening range.
            Returns (0.0, 0.0) if data is unavailable.
        """
        try:
            from tradingagents.strategies.swing_playbook import get_screening_params

            params = get_screening_params(self._config)
            window_min = params.get("orh_window_minutes", 15)

            # Build today's opening range window
            from datetime import datetime as dt
            import pytz
            et = pytz.timezone("US/Eastern")
            today = dt.now(et).date()
            market_open = et.localize(dt.combine(today, dt.strptime("09:30", "%H:%M").time()))
            window_end = market_open + timedelta(minutes=window_min)

            bars = self._data_client.get_intraday_bars(
                symbol=symbol,
                start=market_open,
                end=window_end,
            )

            if bars.empty:
                logger.warning(
                    "%s: no intraday bars for opening range — "
                    "falling back to previous day high/low",
                    symbol,
                )
                return self._fallback_entry_stop(symbol)

            orh = float(bars["high"].max())
            orl = float(bars["low"].min())

            logger.info(
                "%s: Opening Range (first %d min): ORH=$%.2f, ORL=$%.2f",
                symbol, window_min, orh, orl,
            )
            return (round(orh, 2), round(orl, 2))

        except Exception as exc:
            logger.warning("Failed to calculate ORH/ORL for %s: %s", symbol, exc)
            return self._fallback_entry_stop(symbol)

    def _fallback_entry_stop(self, symbol: str) -> tuple:
        """Fallback: use previous day's high/low when intraday data is unavailable."""
        try:
            bars = self._data_client.get_bars(symbol, lookback_days=5)
            import pandas as pd
            if isinstance(bars.index, pd.MultiIndex):
                bars = bars.xs(symbol, level="symbol")

            if bars.empty:
                return (0.0, 0.0)

            # Use last day's high as entry, last 2 days' low as stop
            entry = float(bars["high"].iloc[-1])
            stop = float(bars["low"].tail(2).min())

            if stop >= entry:
                stop = entry * 0.97

            return (round(entry, 2), round(stop, 2))
        except Exception as exc:
            logger.warning("Fallback entry/stop failed for %s: %s", symbol, exc)
            return (0.0, 0.0)

    def _execute_exit(self, action):
        """Execute an exit (full or partial) via Alpaca."""
        try:
            if action.action == "exit_full":
                self._alpaca_client.close_position(action.symbol)
                logger.info("EXIT FULL: %s — %s", action.symbol, action.reason)
                notify("exit", symbol=action.symbol, action="exit_full", reason=action.reason)
            elif action.action == "exit_partial":
                # Get current position to calculate shares to sell
                pos = self._alpaca_client.get_position(action.symbol)
                if pos:
                    import math
                    current_qty = float(pos.qty)
                    sell_qty = math.floor(current_qty * action.exit_pct)
                    if sell_qty > 0:
                        self._alpaca_client.close_position(action.symbol, qty=sell_qty)
                        logger.info(
                            "EXIT PARTIAL: %s — %d/%d shares — %s",
                            action.symbol, sell_qty, int(current_qty), action.reason,
                        )
                        notify("exit", symbol=action.symbol, action="exit_partial", reason=action.reason)
                        # Mark as trimmed in DB
                        if self._trade_db:
                            self._trade_db.mark_trimmed(action.symbol)

            if self._ctx:
                self._ctx.exits_executed.append({
                    "symbol": action.symbol,
                    "action": action.action,
                    "reason": action.reason,
                })
        except Exception as exc:
            logger.error("Exit failed for %s: %s", action.symbol, exc)
            notify("error", message=f"Exit failed for {action.symbol}: {exc}")

    def _execute_stop_update(self, action):
        """Update a stop order to a new price."""
        if action.new_stop is None:
            return
        try:
            # Find the open stop order for this symbol
            orders = self._alpaca_client.get_orders(symbols=[action.symbol])
            for order in orders:
                if hasattr(order, "type") and "stop" in str(order.type).lower():
                    self._alpaca_client.replace_stop_order(
                        order_id=str(order.id),
                        new_stop_price=action.new_stop,
                    )
                    logger.info(
                        "STOP UPDATED: %s -> $%.2f — %s",
                        action.symbol, action.new_stop, action.reason,
                    )
                    notify("stop_update", symbol=action.symbol, new_stop=action.new_stop, reason=action.reason)
                    # Update in DB
                    if self._trade_db:
                        self._trade_db.update_stop(action.symbol, action.new_stop)
                    break
        except Exception as exc:
            logger.error("Stop update failed for %s: %s", action.symbol, exc)
            notify("error", message=f"Stop update failed for {action.symbol}: {exc}")
