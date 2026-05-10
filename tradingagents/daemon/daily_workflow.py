"""Daily workflow: the actual trading logic executed by the scheduler.

Each function maps to a time slot in the trading day:

    9:00 AM  pre_market()     — screener + regime check + pre-filter
    9:45 AM  entry_window()   — run LLM pipeline on filtered tickers → execute entries
   12:00 PM  midday_check()   — Day 3 trims, parabolic extension exits
    3:45 PM  eod_check()      — Day 1 red close, trailing SMA, stop updates
    4:15 PM  post_market()    — daily snapshot, increment day counts, log summary

All functions receive a shared ``DayContext`` that carries state across
the trading day (regime, candidates, etc.).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Dict, List, Optional

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

    # -- 9:00 AM: Pre-market ------------------------------------------------

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

    # -- 9:45 AM: Entry window ----------------------------------------------

    def entry_window(self) -> DayContext:
        """Analyze candidates and execute entries.

        Supports two pipeline modes (configurable via ``pipeline_mode``):

        - **full** — Run multi-agent LLM pipeline concurrently, execute
          only tickers that receive a "Buy" decision.
        - **quant** — Skip LLM entirely; every candidate that passed the
          quantitative pre-filter is automatically a buy.

        In both modes, execution (sizing + ordering) is sequential because
        guardrails check portfolio-level state.
        """
        if self._ctx is None:
            logger.error("entry_window called before pre_market")
            return DayContext()

        if not self._ctx.regime_favorable:
            logger.info("Skipping entries — regime unfavorable")
            return self._ctx

        if not self._ctx.candidates:
            logger.info("Skipping entries — no candidates passed pre-filter")
            return self._ctx

        self._ensure_components()

        candidates = self._ctx.candidates
        pipeline_mode = self._config.get("pipeline_mode", "full")

        # ----- Phase 1: Generate decisions (mode-dependent) ----------------
        if pipeline_mode == "quant":
            pipeline_results = self._quant_decisions(candidates)
        else:
            pipeline_results = self._llm_pipeline_batch(candidates)

        # ----- Phase 2: Sequential execution (shared for both modes) -------
        from tradingagents.execution.executor import Executor, TradeSignal

        executor = Executor(
            alpaca_client=self._alpaca_client,
            trade_db=self._trade_db,
            config=self._config,
            regime=self._ctx.regime,
        )

        # Iterate in original candidate order for deterministic execution
        for symbol in candidates:
            decision = pipeline_results.get(symbol)
            if decision is None:
                continue

            try:
                # Parse decision
                action = self._parse_decision(decision)
                if action != "buy":
                    logger.info("%s: decision is '%s', skipping", symbol, action)
                    # Update screening log with final signal
                    self._trade_db.log_screening_result(
                        date=self._ctx.date, symbol=symbol,
                        source="hybrid_screener", score=1.0,
                        selected_for_pipeline=True, signal_result=action,
                    )
                    continue

                # Calculate entry/stop from recent data
                entry_price, stop_price = self._calculate_entry_stop(symbol)
                if entry_price <= 0 or stop_price <= 0:
                    logger.warning("%s: invalid entry/stop prices", symbol)
                    self._trade_db.log_screening_result(
                        date=self._ctx.date, symbol=symbol,
                        source="hybrid_screener", score=1.0,
                        selected_for_pipeline=True, signal_result="error:prices",
                    )
                    continue

                # Execute
                signal = TradeSignal(
                    symbol=symbol,
                    action="buy",
                    entry_price=entry_price,
                    stop_price=stop_price,
                    rationale=decision[:500] if isinstance(decision, str) else str(decision)[:500],
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
                    logger.info("ENTRY: %s — %d shares @ $%.2f", symbol, result.shares, result.entry_price)
                    self._trade_db.log_screening_result(
                        date=self._ctx.date, symbol=symbol,
                        source="hybrid_screener", score=1.0,
                        selected_for_pipeline=True, signal_result="buy:filled",
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
            "Entry window complete: %d entries submitted (mode=%s)",
            len(self._ctx.entries_submitted), pipeline_mode,
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
        except Exception as exc:
            logger.warning("Failed to record snapshot: %s", exc)

        if self._ctx.errors:
            logger.warning("Day had %d errors: %s", len(self._ctx.errors), self._ctx.errors)

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

    def _calculate_entry_stop(self, symbol: str) -> tuple:
        """Calculate entry and stop prices from recent price data.

        Entry = current price (market entry)
        Stop = Opening Range Low or recent swing low
        """
        try:
            bars = self._data_client.get_bars(symbol, lookback_days=5)
            import pandas as pd
            if isinstance(bars.index, pd.MultiIndex):
                bars = bars.xs(symbol, level="symbol")

            if bars.empty:
                return (0.0, 0.0)

            # Entry = latest close (will be a market order near this level)
            entry = float(bars["close"].iloc[-1])

            # Stop = lowest low of last 2 bars (approximates ORL/LOD)
            stop = float(bars["low"].tail(2).min())

            # Sanity: stop must be below entry
            if stop >= entry:
                stop = entry * 0.97  # fallback: 3% below entry

            return (round(entry, 2), round(stop, 2))

        except Exception as exc:
            logger.warning("Failed to calculate entry/stop for %s: %s", symbol, exc)
            return (0.0, 0.0)

    def _execute_exit(self, action):
        """Execute an exit (full or partial) via Alpaca."""
        try:
            if action.action == "exit_full":
                self._alpaca_client.close_position(action.symbol)
                logger.info("EXIT FULL: %s — %s", action.symbol, action.reason)
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
                    # Update in DB
                    if self._trade_db:
                        self._trade_db.update_stop(action.symbol, action.new_stop)
                    break
        except Exception as exc:
            logger.error("Stop update failed for %s: %s", action.symbol, exc)
