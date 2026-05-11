"""Quick weekend screening test — no orders submitted.

Tests the new pre-filter gates against 10 well-known tickers
to see which ones would pass the playbook criteria.
"""

import os
import sys

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

from tradingagents.screening.pre_filter import PreFilter, check_market_regime

print("=== Market Regime Check ===")
regime = check_market_regime()
print(f"Favorable: {regime['favorable']}")
for k, v in regime.get("checks", {}).items():
    print(f"  {k}: {v}")
if "regime" in regime:
    r = regime["regime"]
    vix = r.get("vix_level", 0)
    print(f"  VIX regime: {r['label']} (VIX={vix:.1f})")

print()
print("=== Pre-Filter Test (10 tickers) ===")
test_tickers = [
    "NVDA", "TSLA", "AAPL", "META", "PLTR",
    "SMCI", "MSTR", "AMD", "COIN", "AMZN",
]
pf = PreFilter()
results = pf.filter_candidates(test_tickers)

print(f"\nPassed: {len(results)}/{len(test_tickers)}")
for r in results:
    uptrend = r.checks.get("prior_uptrend_value", 0)
    rs = r.checks.get("relative_strength_value", 0)
    ma = r.checks.get("ma_stacking", False)
    adr = r.checks.get("adr_pct_value", 0)
    print(
        f"  {r.symbol:6s}  score={r.score:.1f}  "
        f"ADR={adr:.1%}  RS={rs:.1%}  uptrend={uptrend:.0%}  MA_stacked={ma}"
    )

print("\nRejected:")
for sym in test_tickers:
    if not any(r.symbol == sym for r in results):
        rej = pf._evaluate(sym)
        print(f"  {sym:6s}  {rej.reject_reason[:120]}")

print("\n=== Done ===")
