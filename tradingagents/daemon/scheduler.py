"""Trading daemon scheduler: APScheduler-based always-on service.

Usage:
    python -m tradingagents.daemon.scheduler          # run the daemon
    python -m tradingagents.daemon.scheduler --once    # run one full day cycle and exit

The scheduler creates cron jobs for each trading time slot and runs
the ``DailyWorkflow`` at those times on market days.

Schedule (Eastern Time):
    7:55 AM   pre_market       — screener + regime check
    8:05 AM   analyze          — LLM/quant pipeline → store decisions
    9:45 AM   entry_window     — fetch ORH/ORL → submit buy-stop orders
   12:00 PM   midday_check     — Day 3 trims, parabolic exits
    3:45 PM   eod_check        — Day 1 red close, trailing SMA, stops
    4:15 PM   post_market      — daily snapshot + summary
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


class TradingDaemon:
    """APScheduler-based trading daemon.

    Parameters
    ----------
    config : dict, optional
        Configuration dictionary. Defaults to ``DEFAULT_CONFIG``.
    """

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        from tradingagents.default_config import DEFAULT_CONFIG

        self._config = config or DEFAULT_CONFIG
        self._scheduler = None
        self._workflow = None

    def _setup(self):
        """Initialize the scheduler and workflow."""
        from apscheduler.schedulers.blocking import BlockingScheduler
        from apscheduler.triggers.cron import CronTrigger
        from tradingagents.daemon.daily_workflow import DailyWorkflow

        self._workflow = DailyWorkflow(config=self._config)
        self._scheduler = BlockingScheduler(timezone="US/Eastern")

        schedule = self._config.get("trading_schedule", {})

        # Parse schedule times (format: "HH:MM")
        jobs = [
            ("pre_market",   schedule.get("pre_market", "07:55"),   self._workflow.pre_market),
            ("analyze",      schedule.get("analyze", "08:05"),      self._workflow.analyze),
            ("entry_window", schedule.get("entry_window", "09:45"), self._workflow.entry_window),
            ("midday_check", schedule.get("midday_check", "12:00"), self._workflow.midday_check),
            ("eod_check",    schedule.get("eod_check", "15:45"),    self._workflow.eod_check),
            ("post_market",  schedule.get("post_market", "16:15"),  self._workflow.post_market),
        ]

        for job_id, time_str, func in jobs:
            hour, minute = time_str.split(":")
            self._scheduler.add_job(
                func,
                trigger=CronTrigger(
                    day_of_week="mon-fri",
                    hour=int(hour),
                    minute=int(minute),
                    timezone="US/Eastern",
                ),
                id=job_id,
                name=job_id,
                misfire_grace_time=300,  # 5 min grace period
            )
            logger.info("Scheduled: %s at %s ET (Mon-Fri)", job_id, time_str)

    def start(self):
        """Start the daemon (blocking). Runs until Ctrl+C."""
        self._setup()

        # Handle graceful shutdown
        def _shutdown(signum, frame):
            logger.info("Received signal %s — shutting down", signum)
            if self._scheduler:
                self._scheduler.shutdown(wait=False)
            sys.exit(0)

        signal.signal(signal.SIGINT, _shutdown)
        signal.signal(signal.SIGTERM, _shutdown)

        logger.info("=== Trading Daemon STARTED ===")
        logger.info("Schedule: Mon-Fri, US/Eastern")
        logger.info("Press Ctrl+C to stop")
        print("\n[DAEMON] Trading Daemon is running. Press Ctrl+C to stop.\n")

        try:
            self._scheduler.start()
        except (KeyboardInterrupt, SystemExit):
            logger.info("=== Trading Daemon STOPPED ===")

    def run_once(self):
        """Run one complete trading day cycle (for testing/debugging).

        Executes all 5 steps sequentially, regardless of current time.
        """
        from tradingagents.daemon.daily_workflow import DailyWorkflow

        self._workflow = DailyWorkflow(config=self._config)

        pipeline_mode = self._config.get("pipeline_mode", "full")
        print(f"\n[DAEMON] Running one full trading day cycle (pipeline_mode={pipeline_mode})...\n")

        # Step 1: Pre-market
        print("=" * 60)
        print("STEP 1/6: Pre-Market (screener + regime)")
        print("=" * 60)
        ctx = self._workflow.pre_market()
        print(f"  Regime: {ctx.regime.get('label', 'Unknown')} (favorable={ctx.regime_favorable})")
        print(f"  Candidates: {ctx.candidates}")
        print()

        if not ctx.regime_favorable:
            print("[!] Regime unfavorable -- skipping entries")
            print()

        # Step 2: Analyze (LLM/quant pipeline)
        print("=" * 60)
        print("STEP 2/6: Analyze (LLM/quant pipeline)")
        print("=" * 60)
        ctx = self._workflow.analyze()
        buy_count = sum(1 for v in ctx.pipeline_decisions.values() if v == "buy")
        print(f"  Decisions: {dict(ctx.pipeline_decisions)}")
        print(f"  Buy signals: {buy_count}/{len(ctx.pipeline_decisions)}")
        print()

        # Step 3: Entry window (ORH/ORL)
        print("=" * 60)
        print("STEP 3/6: Entry Window (ORH/ORL buy-stop orders)")
        print("=" * 60)
        ctx = self._workflow.entry_window()
        print(f"  Entries submitted: {len(ctx.entries_submitted)}")
        for e in ctx.entries_submitted:
            print(f"    {e['symbol']}: {e['shares']} shares, buy-stop @ ${e['entry']:.2f}, stop @ ${e['stop']:.2f}")
        print()

        # Step 4: Midday check
        print("=" * 60)
        print("STEP 4/6: Midday Check (trims + parabolic exits)")
        print("=" * 60)
        ctx = self._workflow.midday_check()
        print(f"  Exits so far: {len(ctx.exits_executed)}")
        print()

        # Step 5: EOD check
        print("=" * 60)
        print("STEP 5/6: EOD Check (Day 1 red close, trailing SMA)")
        print("=" * 60)
        ctx = self._workflow.eod_check()
        print(f"  Total exits: {len(ctx.exits_executed)}")
        print()

        # Step 6: Post-market
        print("=" * 60)
        print("STEP 6/6: Post-Market Summary")
        print("=" * 60)
        ctx = self._workflow.post_market()
        print()

        # Summary
        print("=" * 60)
        print(f"Day complete: {ctx.date}")
        print(f"  Entries: {len(ctx.entries_submitted)}")
        print(f"  Exits:   {len(ctx.exits_executed)}")
        print(f"  Errors:  {len(ctx.errors)}")
        if ctx.errors:
            for err in ctx.errors:
                print(f"    [!] {err}")
        print("=" * 60)


def main():
    """CLI entry point for the daemon."""
    parser = argparse.ArgumentParser(description="TradingAgents Daemon")
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run one full trading day cycle and exit (for testing)",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging level",
    )
    args = parser.parse_args()

    # Configure logging
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Load env vars
    from dotenv import load_dotenv
    load_dotenv()

    daemon = TradingDaemon()

    if args.once:
        daemon.run_once()
    else:
        daemon.start()


if __name__ == "__main__":
    main()
