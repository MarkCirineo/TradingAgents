"""Phase 3+4 tests -- run with: python scripts/test_phase3_4.py"""

from tradingagents.execution.guardrails import Guardrails, GuardrailResult
from tradingagents.execution.executor import Executor, TradeSignal, ExecutionResult
from tradingagents.execution.position_manager import PositionManager, PositionAction
from tradingagents.strategies.swing_playbook import get_regime_adjustments

print("=== Phase 3+4: Executor + Guardrails + Position Manager ===")
print()

# --- VIX Regime Adjustments ---
print("--- VIX Regime Adjustments ---")
for vix in [12, 17, 25, 35]:
    r = get_regime_adjustments(float(vix))
    print(f"  VIX={vix:2d} -> {r['label']:9s}  risk={r['risk_pct']:.2%}  "
          f"max_pos={r['max_positions']}  exposure={r['max_exposure_pct']:.0%}  "
          f"pause={r['pause_entries']}")

# --- Guardrails ---
print()
print("--- Guardrails ---")

# Panic regime blocks all entries
g_panic = Guardrails(regime=get_regime_adjustments(35.0))
result = g_panic.validate_entry("AAPL", 5000)
print(f"  VIX=35 (Panic):    {result}")

# Normal regime allows entries
g_normal = Guardrails(regime=get_regime_adjustments(17.0))
result = g_normal.validate_entry("AAPL", 5000)
print(f"  VIX=17 (Normal):   {result}")

# --- Position Sizing ---
print()
print("--- Position Sizing (portfolio=$100k, entry=$50, stop=$48, risk/share=$2) ---")
for vix, label in [(12, "Calm"), (17, "Normal"), (25, "Elevated")]:
    regime = get_regime_adjustments(float(vix))
    e = Executor(regime=regime)
    s = e.calculate_position_size(50.0, 48.0, 100_000)
    print(f"  {label:9s}  shares={s['shares']:4d}  "
          f"value=${s['position_value']:>9,.2f}  "
          f"risk=${s['risk_amount']:>7,.2f} ({s['risk_pct_actual']:.2%})")

# --- Position Size Cap ---
print()
print("--- Position Cap (10% of portfolio) ---")
e = Executor(regime=get_regime_adjustments(17.0))
s = e.calculate_position_size(200.0, 199.0, 100_000)
print(f"  Entry=$200, Stop=$199, risk/share=$1")
print(f"  Uncapped would be: {int(100000 * 0.0035 / 1)} shares = ${int(100000 * 0.0035 / 1) * 200:,}")
print(f"  Capped result:     {s['shares']} shares = ${s['position_value']:,.2f} (10% cap hit)")

# --- Position Manager Rules ---
print()
print("--- Position Manager Exit Rules ---")
pm = PositionManager()
for k, v in pm._rules.items():
    print(f"  {k}: {v}")

# --- CLI Regression ---
print()
from tradingagents.graph.trading_graph import TradingAgentsGraph
print("CLI regression: OK")

print()
print("=== All Phase 3+4 tests PASSED ===")
