"""Test the executor + Alpaca bracket order flow directly (no LLM).

Submits a 1-share market buy with stop-loss to confirm the order
pipeline works end-to-end on Alpaca paper.
"""

import os
import sys
from dotenv import load_dotenv

load_dotenv()

# Quick sanity check
if not os.getenv("ALPACA_API_KEY"):
    print("ERROR: ALPACA_API_KEY not set in .env")
    sys.exit(1)

from tradingagents.execution.alpaca_client import AlpacaClient
from tradingagents.execution.executor import Executor, TradeSignal
from tradingagents.execution.trade_db import TradeDB

print("=== Executor Integration Test ===\n")

# 1. Init components
client = AlpacaClient()
db = TradeDB()

print(f"Portfolio value: ${client.get_portfolio_value():,.2f}")
print(f"Positions: {len(client.get_all_positions())}")

# 2. Create executor (no regime = default sizing)
executor = Executor(
    alpaca_client=client,
    trade_db=db,
)

# 3. Create a fake Buy signal with realistic prices
# Get actual current price from Alpaca
from alpaca.data.requests import StockLatestQuoteRequest
from alpaca.data import StockHistoricalDataClient

data_client = StockHistoricalDataClient(
    os.getenv("ALPACA_API_KEY"), os.getenv("ALPACA_SECRET_KEY")
)
test_symbol = "SOUN"  # cheap, liquid stock
quote = data_client.get_stock_latest_quote(StockLatestQuoteRequest(symbol_or_symbols=test_symbol))
bid = float(quote[test_symbol].bid_price)
ask = float(quote[test_symbol].ask_price)
# Use midpoint for entry, stop 10% below bid for safety
current_price = round((bid + ask) / 2, 2) if bid > 0 else ask
stop_price = round(bid * 0.90, 2)  # 10% below bid

print(f"  Bid: ${bid:.2f}, Ask: ${ask:.2f}, Mid: ${current_price:.2f}")

signal = TradeSignal(
    symbol=test_symbol,
    action="buy",
    entry_price=current_price,
    stop_price=stop_price,
    confidence=0.8,
    rationale="TEST: executor integration test",
)

print(f"\nTest signal: BUY {signal.symbol}")
print(f"  Entry: ${signal.entry_price:.2f}")
print(f"  Stop:  ${signal.stop_price:.2f}")
print(f"  Risk per share: ${signal.entry_price - signal.stop_price:.2f}")

# 4. Calculate sizing (just to see what it would do)
sizing = executor.calculate_position_size(
    signal.entry_price, signal.stop_price, client.get_portfolio_value()
)
print(f"\nSizing result: {sizing}")

# 5. Actually execute (will submit to Alpaca paper)
print("\nSubmitting order to Alpaca paper...")
result = executor.execute_entry(signal)

print(f"\nResult:")
print(f"  Success: {result.success}")
print(f"  Shares:  {result.shares}")
print(f"  Entry:   ${result.entry_price:.2f}")
print(f"  Stop:    ${result.stop_price:.2f}")
print(f"  Value:   ${result.position_value:.2f}")
print(f"  Risk:    ${result.risk_amount:.2f}")
print(f"  Order:   {result.order_id}")
if result.reason:
    print(f"  Reason:  {result.reason}")

print("\n=== Done ===")
