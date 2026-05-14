"""Explore consolidation detection on real market data."""
import sys
sys.stdout.reconfigure(encoding="utf-8")

from dotenv import load_dotenv
load_dotenv()

from tradingagents.execution.alpaca_data import AlpacaDataClient
import pandas as pd

dc = AlpacaDataClient()

for symbol in ["AMD", "MSTR", "NVDA"]:
    print(f"\n{'='*60}")
    print(f"  {symbol}")
    print(f"{'='*60}")
    bars = dc.get_bars(symbol, lookback_days=100)
    if bars.empty:
        print("  No data")
        continue
    if isinstance(bars.index, pd.MultiIndex):
        bars = bars.xs(symbol, level="symbol")

    # Compute ADR (14-day average daily range)
    bars["daily_range"] = bars["high"] - bars["low"]
    bars["daily_range_pct"] = bars["daily_range"] / bars["close"]
    adr = bars["daily_range"].tail(14).mean()
    adr_pct = bars["daily_range_pct"].tail(14).mean()
    print(f"  ADR: ${adr:.2f} ({adr_pct:.2%})")

    # Find tight days (range <= 2/3 * ADR) -- doc's definition
    tight_threshold = adr * (2 / 3)
    bars["is_tight"] = bars["daily_range"] <= tight_threshold
    print(f"  Tight threshold: ${tight_threshold:.2f} (2/3 of ADR)")

    # Recent tight days
    last_20 = bars.tail(20).copy()
    tight_in_20 = last_20[last_20["is_tight"]]
    print(f"  Tight days in last 20: {len(tight_in_20)}")

    # Consolidation ceiling = highest high of recent tight days
    if len(tight_in_20) > 0:
        pivot = tight_in_20["high"].max()
        print(f"  Consolidation ceiling (PIVOT): ${pivot:.2f}")
    else:
        pivot = None
        print("  No tight days found (stock not consolidating)")

    # Current price
    current = bars["close"].iloc[-1]
    print(f"  Current price: ${current:.2f}")
    if pivot:
        pct_diff = (pivot - current) / current
        if current < pivot:
            print(f"  --> Price is {abs(pct_diff):.1%} BELOW pivot (breakout pending)")
        else:
            print(f"  --> Price is {abs(pct_diff):.1%} ABOVE pivot (already broken out)")

    # ORH comparison (what we currently use)
    # Use yesterday's high as a rough proxy for ORH
    yesterday_high = bars["high"].iloc[-1]
    print(f"  Yesterday's high (ORH proxy): ${yesterday_high:.2f}")
    if pivot:
        if pivot > yesterday_high:
            print(f"  --> Pivot ${pivot:.2f} > ORH ${yesterday_high:.2f}")
            print(f"     Current ORH would trigger BEFORE the real breakout!")
        else:
            print(f"  --> Pivot ${pivot:.2f} <= ORH ${yesterday_high:.2f}")
            print(f"     ORH already above pivot (breakout may have happened)")

    # Show last 15 days with tight marking
    print(f"\n  Last 15 days:")
    print(f"  {'Date':<12} {'Open':>8} {'High':>8} {'Low':>8} {'Close':>8} {'Range':>7} {'Tight?':>6}")
    print(f"  {'-'*12} {'-'*8} {'-'*8} {'-'*8} {'-'*8} {'-'*7} {'-'*6}")
    for idx, row in bars.tail(15).iterrows():
        d = str(idx)[:10]
        tight_mark = "  <<" if row["is_tight"] else ""
        print(f"  {d:<12} {row['open']:8.2f} {row['high']:8.2f} {row['low']:8.2f} "
              f"{row['close']:8.2f} {row['daily_range']:7.2f}{tight_mark}")

    if pivot:
        print(f"\n  PIVOT LINE: ${pivot:.2f}")
        print(f"  A buy-stop at ${pivot:.2f} only fills when price breaks above the consolidation.")
