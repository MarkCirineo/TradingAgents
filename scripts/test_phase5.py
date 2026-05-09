"""Phase 5 test -- run with: python scripts/test_phase5.py

Tests the daemon imports and DailyWorkflow pre_market step against live data.
Does NOT run entries or exits (that would require the full LLM pipeline).
"""

from dotenv import load_dotenv
load_dotenv()

from tradingagents.daemon.scheduler import TradingDaemon
from tradingagents.daemon.daily_workflow import DailyWorkflow, DayContext

print("=== Phase 5: Daemon Scheduler ===")
print()

# 1. Test pre-market step (screener + regime, live data)
print("--- Pre-Market (live) ---")
wf = DailyWorkflow()
ctx = wf.pre_market()
print(f"  Date: {ctx.date}")
print(f"  Regime favorable: {ctx.regime_favorable}")
if ctx.regime:
    r = ctx.regime
    print(f"  VIX regime: {r.get('label', '?')} "
          f"(risk={r.get('risk_pct', 0):.2%}, "
          f"max_pos={r.get('max_positions', '?')}, "
          f"exposure={r.get('max_exposure_pct', 0):.0%})")
print(f"  Candidates after pre-filter: {ctx.candidates}")

# 2. Test daemon setup (just init, don't start)
print()
print("--- Daemon Init ---")
daemon = TradingDaemon()
print("  TradingDaemon created (not started)")
print("  To run the daemon:  python -m tradingagents.daemon.scheduler")
print("  To test one cycle:  python -m tradingagents.daemon.scheduler --once")

# 3. Regression
print()
from tradingagents.graph.trading_graph import TradingAgentsGraph
print("CLI regression: OK")

print()
print("=== Phase 5 tests PASSED ===")
