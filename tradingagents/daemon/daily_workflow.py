"""Daily workflow: the actual trading logic executed by the scheduler.

Each function maps to a time slot in the trading day:

    7:55 AM  pre_market()     — screener + regime check + pre-filter
    8:05 AM  analyze()        — LLM/quant pipeline → store decisions
    9:30 AM  execute_entries()  — submit buy-stop orders at consolidation pivot
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
    pivot_levels: Dict[str, dict] = field(default_factory=dict)
    quality_scores: Dict[str, float] = field(default_factory=dict)
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

    # -- market calendar ----------------------------------------------------

    def is_trading_day(self, target_date: date = None) -> bool:
        """Check if the given date is a market trading day.

        Uses Alpaca's trading calendar API, which accounts for all US
        market holidays (Memorial Day, July 4th, Thanksgiving, etc.).

        Fails open: if the API is unreachable we assume it IS a trading
        day.  Missing a holiday is far less costly than missing a real
        trading day.
        """
        self._ensure_components()
        target = target_date or date.today()
        try:
            cal = self._alpaca_client.get_calendar(
                start=target.isoformat(),
                end=target.isoformat(),
            )
            if not cal:
                logger.warning(
                    "Empty calendar response for %s — assuming NOT a trading day",
                    target,
                )
                return False
            # Alpaca returns calendar entries only for trading days.
            # If the returned date matches our target, it's a trading day.
            cal_date = getattr(cal[0], "date", None)
            if cal_date:
                cal_date_str = (
                    cal_date.isoformat()
                    if hasattr(cal_date, "isoformat")
                    else str(cal_date)
                )
                is_trading = cal_date_str == target.isoformat()
                if not is_trading:
                    logger.info(
                        "Calendar check: %s is NOT a trading day "
                        "(nearest trading day: %s)",
                        target, cal_date_str,
                    )
                return is_trading
            return True  # fail-open: ambiguous response
        except Exception as exc:
            logger.warning(
                "Calendar check failed for %s: %s — assuming trading day "
                "(fail-open)",
                target, exc,
            )
            return True  # fail-open

    # -- 8:00 AM: Pre-market ------------------------------------------------

    def pre_market(self) -> DayContext:
        """Screen tickers and check market regime.

        This creates the ``DayContext`` for the day. If the regime is
        unfavorable, ``candidates`` will be empty and no entries will
        be attempted.

        If today is a market holiday, returns an empty context
        immediately — all downstream steps (analyze, execute_entries,
        etc.) will see no candidates and no-op.
        """
        self._ensure_components()
        self._ctx = DayContext(date=date.today().isoformat())

        # Market holiday check — skip everything if market is closed today
        if not self.is_trading_day():
            logger.info(
                "=== MARKET HOLIDAY %s — skipping all trading ===",
                self._ctx.date,
            )
            notify(
                "info",
                message=(
                    f"Market holiday {self._ctx.date} — no trading today. "
                    f"All scheduled jobs will be skipped."
                ),
            )
            return self._ctx

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

        portfolio_value = self._current_equity()
        screening_config = self._screening_config_with_dynamic_cap(portfolio_value)
        screener = HybridScreener(
            data_client=self._data_client,
            config=screening_config,
        )
        candidates = screener.scan()

        # 3. Pre-filter
        from tradingagents.screening.pre_filter import PreFilter
        from tradingagents.strategies.swing_playbook import get_sizing_params

        # Size the pre-filter's share gate with the SAME risk the executor
        # will use — VIX-regime-adjusted — so the gate can't desync from live
        # sizing (mirrors Executor: regime["risk_pct"] or the static target).
        effective_risk = self._ctx.regime.get(
            "risk_pct", get_sizing_params(self._config)["target_risk_pct"]
        )
        pf = PreFilter(
            data_client=self._data_client,
            trade_db=self._trade_db,
            config=screening_config,
            portfolio_value=portfolio_value,
            risk_pct=effective_risk,
        )
        symbols = [c.symbol for c in candidates]
        filtered = pf.filter_candidates(symbols)

        self._ctx.candidates = [r.symbol for r in filtered]

        # Preserve quality scores for ranking in analyze() and execute_entries()
        for r in filtered:
            self._ctx.quality_scores[r.symbol] = r.score

        # Extract pivot levels from filter results for use at entry time
        for r in filtered:
            ph = r.checks.get("pivot_high")
            pl = r.checks.get("pivot_low")
            if ph and pl:
                self._ctx.pivot_levels[r.symbol] = {
                    "pivot_high": ph,
                    "pivot_low": pl,
                    "tight_days": r.checks.get("tight_days", 0),
                }

        logger.info(
            "Pre-market: %d screened -> %d passed pre-filter: %s",
            len(candidates),
            len(filtered),
            self._ctx.candidates,
        )
        if self._ctx.quality_scores:
            for sym in self._ctx.candidates:
                qs = self._ctx.quality_scores.get(sym, 0)
                piv = self._ctx.pivot_levels.get(sym)
                if piv:
                    logger.info(
                        "  %s: quality=%.1f, pivot=$%.2f, floor=$%.2f (%d tight days)",
                        sym, qs, piv["pivot_high"], piv["pivot_low"],
                        piv["tight_days"],
                    )
                else:
                    logger.info("  %s: quality=%.1f (no pivot)", sym, qs)

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

    def _current_equity(self) -> Optional[float]:
        """Live account equity, or None if the API call fails.

        None disables the account-size sizing gates (coarse screen cap +
        exact whole-share gate), falling back to the static price band.
        """
        try:
            return self._alpaca_client.get_portfolio_value()
        except Exception as exc:
            logger.warning(
                "Equity unavailable (%s) — account-size sizing gates off, "
                "using static price band", exc,
            )
            return None

    def _screening_config_with_dynamic_cap(
        self, portfolio_value: Optional[float]
    ) -> Dict[str, Any]:
        """Config for the screeners, coarse max_price scaled to the position
        ceiling at the given equity.

        A stock above the position-size ceiling can't fit one whole share, so
        this trims the universe scan to what's tradeable at all.  The exact,
        per-candidate share gate then runs in the pre-filter (see
        ``get_effective_max_price`` / ``calculate_shares``).  Returns
        ``self._config`` unchanged when equity is unknown or the cap doesn't
        bind.
        """
        from tradingagents.strategies.swing_playbook import get_effective_max_price

        if not portfolio_value:
            return self._config

        effective = get_effective_max_price(self._config, portfolio_value)
        static = self._config.get("swing_strategy", {}).get("max_price", 500.0)
        if effective >= static:
            return self._config

        logger.info(
            "Dynamic sizing: coarse screen cap $%.0f (position ceiling at "
            "$%.0f equity, static $%.0f); exact whole-share gate in pre-filter",
            effective, portfolio_value, static,
        )
        config = dict(self._config)
        swing = dict(config.get("swing_strategy", {}))
        swing["max_price"] = effective
        config["swing_strategy"] = swing
        return config

    # -- 8:05 AM: Analyze (LLM pipeline) ------------------------------------

    def analyze(self) -> DayContext:
        """Run the LLM/quant pipeline on candidates and store decisions.

        This runs BEFORE market open to give the LLM plenty of time.
        Decisions are stored in ``self._ctx.pipeline_decisions`` and
        consumed by ``execute_entries()`` at 9:30 AM.
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

        # Sort candidates by quality score (best first) so the LLM pipeline
        # spends its budget (max_pipeline_runs) on the highest-quality setups.
        candidates = sorted(
            self._ctx.candidates,
            key=lambda s: self._ctx.quality_scores.get(s, 0),
            reverse=True,
        )
        pipeline_mode = self._config.get("pipeline_mode", "full")

        logger.info(
            "=== ANALYZE %s (mode=%s, %d candidates) ===",
            self._ctx.date, pipeline_mode, len(candidates),
        )

        if pipeline_mode == "quant":
            pipeline_results = self._quant_decisions(candidates)
        else:
            pipeline_results = self._llm_pipeline_batch(candidates)

        # Store decisions for execute_entries to consume
        for symbol in candidates:
            decision = pipeline_results.get(symbol)
            if decision is not None:
                action = self._parse_decision(decision)
                self._ctx.pipeline_decisions[symbol] = action
                # Update screening log (best-effort — don't abort loop)
                try:
                    self._trade_db.log_screening_result(
                        date=self._ctx.date, symbol=symbol,
                        source="hybrid_screener", score=1.0,
                        selected_for_pipeline=True, signal_result=action,
                    )
                except Exception as exc:
                    logger.warning("Failed to log screening result for %s: %s", symbol, exc)
                logger.info("%s: pipeline decision = %s", symbol, action)

        buy_count = sum(1 for v in self._ctx.pipeline_decisions.values() if v == "buy")
        logger.info(
            "Analysis complete: %d/%d candidates are BUY",
            buy_count, len(candidates),
        )
        return self._ctx

    # -- 9:30 AM: Execute entries (consolidation pivot breakouts) -----------

    def execute_entries(self) -> DayContext:
        """Execute entries using consolidation pivot breakout logic.

        Reads pre-computed decisions from ``analyze()`` and for each
        "buy" signal:
          1. Looks up the consolidation pivot (computed in pre_market)
          2. Checks current price vs pivot to determine order type
          3. Submits buy-stop at pivot_high or market order if confirmed

        The buy-stop only fills if price breaks above the consolidation
        ceiling during the day. If the pivot is never breached, the
        order expires at market close.
        """
        if self._ctx is None:
            logger.error("execute_entries called before pre_market")
            return DayContext()

        # Second-line-of-defense: confirm market is open before placing orders.
        # pre_market() already checks, but this guards against manual --once
        # invocations or scheduler edge cases where pre_market was skipped.
        if not self.is_trading_day():
            logger.warning(
                "execute_entries called on non-trading day — aborting"
            )
            return self._ctx

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

        # Sort buy candidates by quality score (best first) so the
        # highest-quality setups get first dibs on limited position slots.
        # Guardrails will naturally block excess entries once limits are hit.
        buy_symbols = sorted(
            [s for s, d in self._ctx.pipeline_decisions.items() if d == "buy"],
            key=lambda s: self._ctx.quality_scores.get(s, 0),
            reverse=True,
        )
        logger.info(
            "Execute entries: %d BUY decisions to execute via pivot breakout "
            "(quality-ranked)",
            len(buy_symbols),
        )
        for symbol in buy_symbols:
            logger.info(
                "  %s: quality=%.1f",
                symbol, self._ctx.quality_scores.get(symbol, 0),
            )

        for symbol in buy_symbols:
            try:
                # 1. Look up pre-computed consolidation pivot
                pivot = self._ctx.pivot_levels.get(symbol)
                if not pivot:
                    logger.info(
                        "%s: no consolidation pivot — SKIP (not A+ setup)",
                        symbol,
                    )
                    self._trade_db.log_screening_result(
                        date=self._ctx.date, symbol=symbol,
                        source="hybrid_screener", score=1.0,
                        selected_for_pipeline=True, signal_result="skip:no_pivot",
                    )
                    continue

                pivot_high = pivot["pivot_high"]
                pivot_low = pivot["pivot_low"]
                risk_per_share = pivot_high - pivot_low

                if risk_per_share <= 0:
                    logger.warning(
                        "%s: invalid pivot range (high=$%.2f, low=$%.2f) — SKIP",
                        symbol, pivot_high, pivot_low,
                    )
                    self._trade_db.log_screening_result(
                        date=self._ctx.date, symbol=symbol,
                        source="hybrid_screener", score=1.0,
                        selected_for_pipeline=True, signal_result="skip:invalid_pivot_range",
                    )
                    continue

                logger.info(
                    "%s: pivot=$%.2f, floor=$%.2f, risk=$%.2f (%d tight days)",
                    symbol, pivot_high, pivot_low, risk_per_share,
                    pivot.get("tight_days", 0),
                )

                # 2. Get current price
                try:
                    snap = self._data_client.get_snapshots([symbol])
                    current_price = float(snap[symbol].latest_trade.price)
                except Exception as exc:
                    logger.warning(
                        "%s: price lookup failed (%s) — skipping entry",
                        symbol, exc,
                    )
                    self._trade_db.log_screening_result(
                        date=self._ctx.date, symbol=symbol,
                        source="hybrid_screener", score=1.0,
                        selected_for_pipeline=True, signal_result="skip:price_unavailable",
                    )
                    continue

                # 3. Determine order type based on current price vs pivot
                if current_price < pivot_low:
                    # Broke below consolidation floor — setup invalidated
                    logger.info(
                        "%s: price $%.2f < floor $%.2f — breakdown, SKIP",
                        symbol, current_price, pivot_low,
                    )
                    self._trade_db.log_screening_result(
                        date=self._ctx.date, symbol=symbol,
                        source="hybrid_screener", score=1.0,
                        selected_for_pipeline=True, signal_result="skip:breakdown",
                    )
                    continue

                elif current_price <= pivot_high:
                    # Still in consolidation — buy-stop at pivot
                    entry_type = "stop"
                    entry_price = pivot_high
                    logger.info(
                        "%s: price $%.2f <= pivot $%.2f — buy-stop at pivot",
                        symbol, current_price, pivot_high,
                    )

                elif current_price <= pivot_high + risk_per_share:
                    # Breakout confirmed but not extended — market order
                    # Doc line 370: "I always use mrkt orders"
                    entry_type = "market"
                    entry_price = round(current_price, 2)
                    logger.info(
                        "%s: price $%.2f > pivot $%.2f (breakout confirmed) — "
                        "market order",
                        symbol, current_price, pivot_high,
                    )

                else:
                    # Too extended above pivot — chasing territory
                    # Doc line 815: "Do not chase"
                    logger.info(
                        "%s: price $%.2f >> pivot $%.2f (extended by $%.2f > "
                        "risk $%.2f) — SKIP (chasing)",
                        symbol, current_price, pivot_high,
                        current_price - pivot_high, risk_per_share,
                    )
                    self._trade_db.log_screening_result(
                        date=self._ctx.date, symbol=symbol,
                        source="hybrid_screener", score=1.0,
                        selected_for_pipeline=True, signal_result="skip:too_extended",
                    )
                    continue

                signal = TradeSignal(
                    symbol=symbol,
                    action="buy",
                    entry_price=entry_price,
                    stop_price=pivot_low,  # stop at consolidation floor
                    rationale=(
                        f"Pivot breakout ({entry_type}): entry @ ${entry_price:.2f}, "
                        f"stop @ ${pivot_low:.2f}, tight_days={pivot.get('tight_days', 0)}"
                    ),
                    entry_type=entry_type,
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
                        "ENTRY: %s — %d shares, %s @ $%.2f, stop @ $%.2f",
                        symbol, result.shares, entry_type,
                        result.entry_price, result.stop_price,
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

        # Immediately confirm fills for market-order entries (breakout
        # already confirmed at submission); buy-stops stay PENDING until
        # a later reconciliation pass sees them trigger.
        self._reconcile_pending_entries()

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

        # Promote buy-stop entries that triggered this morning to OPEN
        # (and pick up their actual fill prices) before managing positions.
        self._reconcile_pending_entries()

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

        # Final fill check for the day.  Unfilled buy-stop entries get
        # cancelled here: the pivot breakout didn't happen today, so the
        # setup is dead (the GTC entry order would otherwise linger and
        # could fire days later on a stale signal).
        self._reconcile_pending_entries(cancel_unfilled=True)

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

        # Catch any entry fills/cancellations not yet synced (safety net
        # in case eod_check didn't run — e.g. daemon restart).
        self._reconcile_pending_entries(cancel_unfilled=True)

        # Reconcile our DB with Alpaca's actual positions.
        # This cleans up stale test data and positions closed externally.
        self._reconcile_positions()

        # Safety net: ensure every open position has a GTC stop order.
        # With GTC brackets (Change #5), positions already have baseline
        # protection.  This catches edge cases where EOD stop updates
        # cancelled the old stop but failed to place a new one.
        self._ensure_stops()

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

            # Send daily summary notification (with actual P&L)
            snapshot = self._trade_db.get_daily_snapshot(self._ctx.date)
            actual_pnl = snapshot.get("daily_pnl", 0) if snapshot else 0
            notify(
                "daily_summary",
                portfolio=portfolio_value,
                entries=len(self._ctx.entries_submitted),
                exits=len(self._ctx.exits_executed),
                positions=num_positions,
                pnl=actual_pnl,
            )
        except Exception as exc:
            logger.warning("Failed to record snapshot: %s", exc)

        if self._ctx.errors:
            logger.warning("Day had %d errors: %s", len(self._ctx.errors), self._ctx.errors)
            notify("error", message=f"Day had {len(self._ctx.errors)} errors: {', '.join(self._ctx.errors[:3])}")

        logger.info("=== POST-MARKET %s COMPLETE ===", self._ctx.date)
        return self._ctx

    # -- reconciliation -----------------------------------------------------

    def _reconcile_pending_entries(self, cancel_unfilled: bool = False):
        """Sync PENDING positions with their Alpaca entry-order status.

        Positions are recorded as PENDING at submission (a buy-stop entry
        may never trigger).  This polls the entry order and:

        - **filled** -> promotes to OPEN with the actual fill price/date
        - **cancelled/expired/rejected** -> retires the row quietly
          (no phantom position for exit logic to trip over)
        - **still working** -> leaves it pending; with *cancel_unfilled*
          (EOD), cancels the order first — a pivot breakout that didn't
          trigger the same day is a dead setup, and the GTC entry would
          otherwise linger and fire days later.
        """
        if not (self._trade_db and self._alpaca_client):
            return

        pending = self._trade_db.get_pending_positions()
        if not pending:
            return

        _DEAD = {"canceled", "cancelled", "expired", "rejected", "replaced"}

        for pos in pending:
            symbol = pos["symbol"]
            order_id = pos.get("entry_order_id") or pos.get("stop_order_id")
            if not order_id:
                # Legacy row with no order reference — assume filled
                # (pre-fill-tracking behavior).
                self._trade_db.mark_position_filled(symbol)
                continue

            try:
                order = self._alpaca_client.get_order_by_id(order_id)
            except Exception as exc:
                logger.warning(
                    "Fill check failed for %s (order %s): %s",
                    symbol, order_id, exc,
                )
                continue

            status = str(getattr(order, "status", "")).lower().replace(
                "orderstatus.", ""
            )
            filled_qty = float(getattr(order, "filled_qty", None) or 0)

            if status == "filled" or filled_qty > 0:
                raw_price = getattr(order, "filled_avg_price", None)
                fill_price = float(raw_price) if raw_price else None
                filled_at = getattr(order, "filled_at", None)
                fill_date = (
                    filled_at.date().isoformat()
                    if hasattr(filled_at, "date") else None
                )
                self._trade_db.mark_position_filled(
                    symbol,
                    fill_price=fill_price,
                    fill_date=fill_date,
                    fill_qty=filled_qty if 0 < filled_qty < pos["current_qty"] else None,
                )
                self._trade_db.update_order_status(order_id, "FILLED", fill_price)
                logger.info(
                    "ENTRY CONFIRMED: %s — %d shares filled @ $%.2f",
                    symbol, int(filled_qty or pos["current_qty"]),
                    fill_price or pos["entry_price"],
                )

            elif status in _DEAD:
                self._trade_db.cancel_pending_position(symbol)
                self._trade_db.update_order_status(order_id, "CANCELLED")
                logger.info(
                    "ENTRY NEVER FILLED: %s — order %s (%s), position retired",
                    symbol, order_id, status,
                )

            elif cancel_unfilled:
                try:
                    self._alpaca_client.cancel_order(order_id)
                    self._trade_db.cancel_pending_position(symbol)
                    self._trade_db.update_order_status(order_id, "CANCELLED")
                    logger.info(
                        "ENTRY CANCELLED (EOD): %s — pivot never triggered today",
                        symbol,
                    )
                except Exception as exc:
                    logger.warning(
                        "Failed to cancel unfilled entry for %s: %s", symbol, exc
                    )

    def _reconcile_positions(self):
        """Sync trade_db positions with Alpaca's actual positions.

        Any position marked OPEN in our DB that doesn't exist in Alpaca
        gets closed with reason ``EXTERNAL_SYNC``.  This handles:

        - Manual sells via Alpaca UI
        - Stop-loss fills we didn't track
        - Stale test data
        """
        if not self._trade_db or not self._alpaca_client:
            return

        db_open = self._trade_db.get_open_positions()
        if not db_open:
            return

        # Get actual positions from Alpaca
        try:
            alpaca_positions = self._alpaca_client.get_all_positions()
            alpaca_symbols = {p.symbol for p in alpaca_positions}
        except Exception as exc:
            logger.warning("Reconciliation skipped — could not fetch Alpaca positions: %s", exc)
            return

        synced = 0
        for pos in db_open:
            if pos["symbol"] not in alpaca_symbols:
                self._trade_db.close_position(pos["symbol"], "EXTERNAL_SYNC")
                logger.warning(
                    "SYNC: closed stale DB position %s (not in Alpaca)",
                    pos["symbol"],
                )
                synced += 1

        if synced:
            logger.info("Reconciled %d stale position(s)", synced)
        else:
            logger.debug("Position reconciliation: all %d DB positions match Alpaca", len(db_open))

    def _ensure_stops(self):
        """Safety net: ensure every open position has a GTC stop order.

        Runs in ``post_market`` after reconciliation.  With GTC brackets,
        positions already have baseline stop protection from entry.  This
        catches edge cases where EOD stop updates cancelled the old stop
        but failed to place a new one.

        Stop price selection (best available):
        1. Today's Low of Day (LOD) from market data — this is what the
           PositionManager would have set if the update hadn't failed.
        2. DB ``current_stop`` (or legacy ``entry_orl``) — the last
           known stop if no update ever succeeded.
        3. Skip if neither is available.
        """
        if not self._alpaca_client or not self._trade_db:
            return

        from alpaca.trading.enums import OrderSide

        try:
            positions = self._alpaca_client.get_all_positions()
        except Exception as exc:
            logger.warning("_ensure_stops skipped — could not fetch positions: %s", exc)
            return

        for pos in positions:
            symbol = pos.symbol
            try:
                open_orders = self._alpaca_client.get_orders(symbols=[symbol])
                has_stop = any(
                    "stop" in str(getattr(o, "type", "")).lower()
                    for o in open_orders
                )
                if has_stop:
                    continue  # already protected

                # Determine the best stop price:
                # Prefer today's LOD (what position manager intended)
                # over the last tracked DB stop.
                db_pos = self._trade_db.get_position(symbol)
                db_stop = (
                    (db_pos.get("current_stop") or db_pos.get("entry_orl", 0))
                    if db_pos else 0
                )

                # Fetch today's LOD from market data
                lod = 0.0
                try:
                    bars = self._data_client.get_bars(symbol, lookback_days=5)
                    import pandas as pd
                    if isinstance(bars.index, pd.MultiIndex):
                        bars = bars.xs(symbol, level="symbol")
                    if not bars.empty:
                        lod = float(bars["low"].iloc[-1])
                except Exception as data_exc:
                    logger.warning(
                        "SAFETY NET: could not fetch LOD for %s: %s",
                        symbol, data_exc,
                    )

                # Use LOD if it's a valid price, otherwise fall back to DB stop
                if lod > 0:
                    stop_price = lod
                    stop_source = "LOD"
                elif db_stop > 0:
                    stop_price = db_stop
                    stop_source = "DB stop"
                else:
                    logger.warning(
                        "SAFETY NET: %s has no stop and no DB stop price — SKIPPED",
                        symbol,
                    )
                    continue

                stop_price = round(stop_price, 2)
                qty = float(pos.qty)
                new_order = self._alpaca_client.submit_stop_order(
                    symbol=symbol,
                    qty=qty,
                    side=OrderSide.SELL,
                    stop_price=stop_price,
                    # GTC is the default
                )
                logger.warning(
                    "SAFETY NET: placed GTC stop for %s @ $%.2f (source=%s), qty=%d "
                    "(no existing stop found) — order %s",
                    symbol, stop_price, stop_source, int(qty),
                    getattr(new_order, "id", "?"),
                )
                notify(
                    "stop_update",
                    symbol=symbol,
                    new_stop=stop_price,
                    reason=f"safety net ({stop_source}): no stop found in post_market",
                )

                # Update DB to reflect the stop we actually placed
                if self._trade_db and db_pos:
                    self._trade_db.update_stop(symbol, stop_price, stop_type="lod")
                    self._trade_db.update_position(
                        symbol,
                        stop_order_id=str(getattr(new_order, "id", "")),
                    )

            except Exception as exc:
                logger.error(
                    "SAFETY NET: failed to ensure stop for %s: %s",
                    symbol, exc,
                )

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

    def _cancel_open_orders(self, symbol):
        """Cancel all open orders for a symbol.

        Bracket children (stop-loss / take-profit) lock the shares, so they
        must be cancelled before the position can be closed or trimmed.
        """
        try:
            open_orders = self._alpaca_client.get_orders(symbols=[symbol])
        except Exception as exc:
            logger.warning("Could not fetch/cancel orders for %s: %s", symbol, exc)
            return
        for order in open_orders:
            try:
                self._alpaca_client.cancel_order(str(order.id))
                logger.info(
                    "Cancelled order %s (%s) for %s before exit",
                    order.id, getattr(order, "type", "?"), symbol,
                )
            except Exception as cancel_exc:
                logger.warning("Could not cancel order %s: %s", order.id, cancel_exc)

    def _replace_stop_after_trim(self, symbol, qty):
        """Re-place a GTC stop on the shares left after a partial trim.

        Trimming cancels the bracket stop to free the shares, so the remainder
        would otherwise sit unprotected until the post_market safety net.  Use
        the position's known stop level (DB ``current_stop``, else the initial
        ORL).  ``qty`` is the computed remainder, so the stop never covers more
        shares than are held regardless of fill-settlement timing.  On any
        failure, ``_ensure_stops`` in post_market remains the backstop.
        """
        if qty < 1 or not self._trade_db:
            return
        db_pos = self._trade_db.get_position(symbol)
        stop_price = (
            (db_pos.get("current_stop") or db_pos.get("entry_orl", 0))
            if db_pos else 0
        )
        if not stop_price or stop_price <= 0:
            logger.warning(
                "Post-trim stop for %s skipped — no known stop price; "
                "post_market safety net will cover", symbol,
            )
            return
        from alpaca.trading.enums import OrderSide
        try:
            order = self._alpaca_client.submit_stop_order(
                symbol=symbol, qty=qty, side=OrderSide.SELL,
                stop_price=round(stop_price, 2),
            )
            logger.info(
                "Re-placed GTC stop for %s remainder — %d sh @ $%.2f after trim",
                symbol, int(qty), round(stop_price, 2),
            )
            self._trade_db.update_position(
                symbol, stop_order_id=str(getattr(order, "id", "")),
            )
        except Exception as exc:
            logger.error(
                "Failed to re-place stop after trim for %s: %s — "
                "post_market safety net will cover", symbol, exc,
            )

    def _execute_exit(self, action):
        """Execute an exit (full or partial) via Alpaca.

        Full exit: cancel the bracket children (they lock the shares) and
        close.  Partial trim: compute the whole-share sell quantity FIRST — if
        it rounds to 0 (e.g. a 1-share position) do nothing and leave the
        protective stop in place; otherwise cancel, sell, and immediately
        re-place a stop on the remaining shares.
        """
        import time

        try:
            if action.action == "exit_full":
                self._cancel_open_orders(action.symbol)
                # Retry with backoff — Alpaca processes bracket child
                # cancellations asynchronously, shares may still be locked.
                max_attempts = 5
                for attempt in range(1, max_attempts + 1):
                    try:
                        self._alpaca_client.close_position(action.symbol)
                        logger.info("EXIT FULL: %s — %s", action.symbol, action.reason)
                        notify("exit", symbol=action.symbol, action="exit_full", reason=action.reason)
                        break
                    except Exception as close_exc:
                        close_msg = str(close_exc).lower()
                        is_retryable = any(
                            kw in close_msg
                            for kw in ("cannot", "locked", "available", "insufficient", "held")
                        )
                        if attempt < max_attempts and is_retryable:
                            wait = 1.0 * attempt  # 1s, 2s, 3s, 4s
                            logger.warning(
                                "close_position attempt %d/%d for %s failed (%s) "
                                "— retrying in %.0fs",
                                attempt, max_attempts, action.symbol,
                                close_exc, wait,
                            )
                            time.sleep(wait)
                        else:
                            raise  # re-raise to hit the outer except handler

            elif action.action == "exit_partial":
                # Compute the whole-share sell qty from the LIVE position
                # BEFORE touching any orders: if it rounds to 0 we must not
                # strip the protective stop for a sale that won't happen.
                pos = self._alpaca_client.get_position(action.symbol)
                if not pos:
                    logger.info(
                        "EXIT PARTIAL: %s — no live position, nothing to trim",
                        action.symbol,
                    )
                    return
                import math
                current_qty = float(pos.qty)
                sell_qty = math.floor(current_qty * action.exit_pct)
                if sell_qty < 1:
                    logger.info(
                        "Trim skipped for %s — %d share(s) too few to sell "
                        "%.0f%%; holding full position, stop left intact",
                        action.symbol, int(current_qty), action.exit_pct * 100,
                    )
                    return  # protective stop NOT cancelled

                self._cancel_open_orders(action.symbol)
                time.sleep(1.0)  # let cancellations settle before selling
                self._alpaca_client.close_position(action.symbol, qty=sell_qty)
                remaining = int(current_qty) - sell_qty
                logger.info(
                    "EXIT PARTIAL: %s — %d/%d shares — %s",
                    action.symbol, sell_qty, int(current_qty), action.reason,
                )
                notify("exit", symbol=action.symbol, action="exit_partial", reason=action.reason)
                if self._trade_db:
                    self._trade_db.mark_trimmed(action.symbol)
                # Re-protect the remainder now; don't wait for post_market.
                self._replace_stop_after_trim(action.symbol, remaining)

            if self._ctx:
                self._ctx.exits_executed.append({
                    "symbol": action.symbol,
                    "action": action.action,
                    "reason": action.reason,
                })

            # Sync our DB regardless of exit type
            if self._trade_db and action.action == "exit_full":
                self._trade_db.close_position(action.symbol, action.reason)

        except Exception as exc:
            error_msg = str(exc).lower()
            if "not found" in error_msg or "available" in error_msg or "no position" in error_msg:
                # Position already closed externally — sync our DB
                logger.warning(
                    "Position %s already closed externally — syncing DB",
                    action.symbol,
                )
                if self._trade_db:
                    self._trade_db.close_position(action.symbol, "EXTERNAL_SYNC")
                if self._ctx:
                    self._ctx.exits_executed.append({
                        "symbol": action.symbol,
                        "action": action.action,
                        "reason": f"{action.reason} (synced: already closed)",
                    })
            else:
                logger.error("Exit failed for %s: %s", action.symbol, exc)
                notify("error", message=f"Exit failed for {action.symbol}: {exc}")

    def _execute_stop_update(self, action):
        """Update a stop by cancelling existing orders and placing a new GTC stop.

        We cancel-and-resubmit rather than using ``replace_stop_order``
        because this approach is idempotent — it works regardless of
        whether the existing orders are bracket children from entry or
        standalone stops from a previous update.  It also produces a
        clean, independent GTC stop with a known order ID we track in
        the DB, and removes stale take-profit ceilings so that
        PositionManager has full exit control.

        Steps:
        1. Cancel ALL open orders for the symbol
        2. Wait for Alpaca to process cancellations
        3. Submit a new standalone GTC stop order
        4. Update the DB with the new stop price and order ID
        """
        if action.new_stop is None:
            return

        import time
        from alpaca.trading.enums import OrderSide

        try:
            # 1. Cancel all open orders for this symbol (bracket children)
            orders = self._alpaca_client.get_orders(symbols=[action.symbol])
            cancelled_ids = []
            for order in orders:
                try:
                    self._alpaca_client.cancel_order(str(order.id))
                    logger.info(
                        "Cancelled %s order %s for %s (stop update)",
                        getattr(order, "type", "?"), order.id, action.symbol,
                    )
                    cancelled_ids.append(str(order.id))
                except Exception as cancel_exc:
                    logger.warning(
                        "Could not cancel order %s: %s", order.id, cancel_exc,
                    )

            # 1b. Poll until Alpaca confirms all cancellations are processed.
            # Alpaca processes bracket-child cancellations asynchronously;
            # a blind 1s sleep is unreliable.  Poll up to 5× with backoff.
            if cancelled_ids:
                from alpaca.trading.enums import OrderStatus as _OS
                max_polls = 5
                for poll in range(1, max_polls + 1):
                    time.sleep(1.0 * poll)  # 1s, 2s, 3s, 4s, 5s
                    still_open = self._alpaca_client.get_orders(
                        symbols=[action.symbol],
                    )
                    if not still_open:
                        logger.info(
                            "All orders for %s confirmed cancelled (poll %d/%d)",
                            action.symbol, poll, max_polls,
                        )
                        break
                    if poll == max_polls:
                        logger.warning(
                            "%d order(s) still open for %s after %d polls — "
                            "proceeding anyway",
                            len(still_open), action.symbol, max_polls,
                        )

            # 2. Get current position to determine qty for the new stop
            pos = self._alpaca_client.get_position(action.symbol)
            if not pos:
                logger.warning(
                    "No Alpaca position for %s — cannot place new stop",
                    action.symbol,
                )
                return

            qty = float(pos.qty)

            # Round to 2 decimal places — Alpaca rejects sub-penny prices
            # (SEC Rule 612) and SMA values are rolling means with fractional cents.
            rounded_stop = round(action.new_stop, 2)

            # 3. Submit new standalone GTC stop order (with retry)
            max_submit = 3
            new_order = None
            for attempt in range(1, max_submit + 1):
                try:
                    new_order = self._alpaca_client.submit_stop_order(
                        symbol=action.symbol,
                        qty=qty,
                        side=OrderSide.SELL,
                        stop_price=rounded_stop,
                        # GTC is the default in submit_stop_order
                    )
                    break
                except Exception as submit_exc:
                    submit_msg = str(submit_exc).lower()
                    is_retryable = any(
                        kw in submit_msg
                        for kw in ("exclusive", "insufficient", "cannot", "held", "locked")
                    )
                    if attempt < max_submit and is_retryable:
                        wait = 2.0 * attempt
                        logger.warning(
                            "Stop submit attempt %d/%d for %s failed (%s) "
                            "— retrying in %.0fs",
                            attempt, max_submit, action.symbol,
                            submit_exc, wait,
                        )
                        time.sleep(wait)
                    else:
                        raise  # re-raise to outer handler

            if new_order is None:
                logger.error("Stop submission returned None for %s", action.symbol)
                return

            logger.info(
                "STOP UPDATED: %s -> $%.2f (new GTC stop %s) — %s",
                action.symbol, rounded_stop,
                getattr(new_order, "id", "?"), action.reason,
            )
            notify(
                "stop_update",
                symbol=action.symbol,
                new_stop=rounded_stop,
                reason=action.reason,
            )

            # 4. Determine stop type from reason for audit trail
            if "breakeven" in action.reason.lower():
                stop_type = "breakeven"
            elif "trailing" in action.reason.lower() or "SMA" in action.reason:
                stop_type = "trailing"
            elif "LOD" in action.reason:
                stop_type = "lod"
            else:
                stop_type = ""

            # Update in DB with stop type flag and new order ID
            if self._trade_db:
                self._trade_db.update_stop(
                    action.symbol, action.new_stop, stop_type=stop_type,
                )
                # Track the new stop order ID
                self._trade_db.update_position(
                    action.symbol,
                    stop_order_id=str(getattr(new_order, "id", "")),
                )

        except Exception as exc:
            logger.error("Stop update failed for %s: %s", action.symbol, exc)
            notify("error", message=f"Stop update failed for {action.symbol}: {exc}")
