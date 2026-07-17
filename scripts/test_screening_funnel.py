"""End-to-end screening funnel test: regime -> screener -> pre-filter.

Read-only, no orders. Runs the exact same path pre_market() uses and
shows how many candidates survive each stage and why.

Usage:
    python scripts/test_screening_funnel.py            # full funnel
    python scripts/test_screening_funnel.py --rejects  # also show per-gate reject reasons
"""

import os
import sys
from collections import Counter

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

import logging
logging.basicConfig(level=logging.WARNING)

show_rejects = "--rejects" in sys.argv

from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.execution.alpaca_data import AlpacaDataClient
from tradingagents.screening.screener import HybridScreener
from tradingagents.screening.pre_filter import PreFilter, check_market_regime

data_client = AlpacaDataClient()

print("=== Stage 1: Market regime ===")
regime = check_market_regime(data_client=data_client)
print(f"Favorable: {regime['favorable']}")
for k, v in regime.get("checks", {}).items():
    print(f"  {k}: {v}")

print("\n=== Stage 2: Screener universe ===")
candidates = HybridScreener(data_client=data_client, config=DEFAULT_CONFIG).scan()
print(f"{len(candidates)} candidates (cap={DEFAULT_CONFIG['screening']['max_candidates']}):")
for c in candidates:
    print(f"  {c.symbol:<6} score={c.score:.2f}  [{c.source}]")

print("\n=== Stage 3: Pre-filter (takes a few minutes) ===")
pf = PreFilter(data_client=data_client)
symbols = [c.symbol for c in candidates]

gate_fails = Counter()
passed = []
for sym in symbols:
    r = pf._evaluate(sym)
    if r.passed:
        passed.append(r)
    else:
        for gate, ok in r.checks.items():
            if ok is False:
                gate_fails[gate] += 1
        if show_rejects:
            print(f"  REJECT {sym:<6} {r.reject_reason}")

passed.sort(key=lambda r: r.score, reverse=True)
print(f"\nPASSED: {len(passed)}/{len(symbols)}")
for r in passed:
    ch = r.checks
    print(
        f"  {r.symbol:<6} quality={r.score:5.1f}  "
        f"RS={ch.get('relative_strength_value', 0):+.1%}  "
        f"uptrend={ch.get('prior_uptrend_value', 0):+.1%}  "
        f"ADR={ch.get('adr_pct_value', 0):.1%}  "
        f"pivot={ch.get('pivot_high')}/{ch.get('pivot_low')}"
    )

print("\nGate failure counts:")
for gate, n in gate_fails.most_common():
    print(f"  {gate:<22} {n}")
