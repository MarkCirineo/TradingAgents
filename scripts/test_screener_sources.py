"""Live smoke test for the multi-source screener — read-only, no orders.

Runs each screening source (Alpaca most-actives, Alpaca movers, Yahoo
Finance criteria screener) plus the hybrid merge, and prints the
candidate universe each produces.
"""

import os
import sys

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

import logging
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.execution.alpaca_data import AlpacaDataClient
from tradingagents.screening.screener import (
    AlpacaMoversScreener,
    AlpacaScreener,
    HybridScreener,
    YFinanceScreener,
)

data_client = AlpacaDataClient()

print("\n=== Alpaca most-actives (existing source) ===")
for c in AlpacaScreener(data_client=data_client).scan(top=10):
    print(f"  {c.symbol:<6} score={c.score:.2f}  {c.reason}")

print("\n=== Alpaca movers / top gainers (new) ===")
for c in AlpacaMoversScreener(data_client=data_client).scan(top=10):
    print(f"  {c.symbol:<6} score={c.score:.2f}  {c.reason}")

print("\n=== Yahoo Finance criteria screener (new) ===")
yf_candidates = YFinanceScreener(config=DEFAULT_CONFIG).scan()
print(f"  total: {len(yf_candidates)}")
for c in yf_candidates[:15]:
    print(f"  {c.symbol:<6} score={c.score:.2f}  {c.reason}")

print("\n=== Hybrid merge (what pre_market() will now see) ===")
merged = HybridScreener(data_client=data_client, config=DEFAULT_CONFIG).scan()
print(f"  total: {len(merged)} (cap={DEFAULT_CONFIG['screening']['max_candidates']})")
for c in merged:
    print(f"  {c.symbol:<6} score={c.score:.2f}  [{c.source}]")

multi = [c for c in merged if "+" in c.source]
print(f"\n  multi-source hits: {len(multi)}: {[c.symbol for c in multi]}")
