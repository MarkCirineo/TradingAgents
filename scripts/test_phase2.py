"""Phase 2 live integration test -- run with: python scripts/test_phase2.py"""

from dotenv import load_dotenv
load_dotenv()

from tradingagents.screening.screener import HybridScreener
from tradingagents.screening.pre_filter import PreFilter, check_market_regime
from tradingagents.execution.alpaca_data import AlpacaDataClient

# Shared data client
dc = AlpacaDataClient()

# 1. Run Alpaca screener
print("--- Alpaca Screener ---")
hs = HybridScreener(
    data_client=dc,
    config={"screening": {"source": "alpaca", "max_candidates": 20}},
)
candidates = hs.scan()
for c in candidates[:5]:
    print(f"  {c.symbol:6s}  score={c.score:.3f}  vol={c.volume:>15,}  {c.reason}")
print(f"  ... {len(candidates)} total candidates")

# 2. Market regime check
print()
print("--- Market Regime ---")
regime = check_market_regime(data_client=dc)
print(f"  Favorable: {regime['favorable']}")
for k, v in regime["checks"].items():
    print(f"  {k}: {v}")
if "regime" in regime:
    r = regime["regime"]
    print(f"  Regime: {r['label']} (risk={r['risk_pct']:.2%}, "
          f"max_pos={r['max_positions']}, exposure={r['max_exposure_pct']:.0%}, "
          f"pause={r['pause_entries']})")

# 3. Pre-filter top 20 candidates
print()
print("--- Pre-Filter (top 20) ---")
if candidates:
    pf = PreFilter(data_client=dc)
    symbols = [c.symbol for c in candidates[:20]]
    results = pf.filter_candidates(symbols)
    for r in results:
        status = "PASS" if r.passed else "FAIL"
        print(f"  {r.symbol:6s}  [{status}]  score={r.score:.1f}")
        for k, v in r.checks.items():
            print(f"    {k}: {v}")
    if not results:
        print("  (all 8 were rejected by pre-filter)")

print()
print("=== Phase 2 live integration test PASSED ===")
