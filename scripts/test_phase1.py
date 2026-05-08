"""Phase 1 smoke test -- run with: python scripts/test_phase1.py"""

from tradingagents.execution.alpaca_client import (
    AlpacaClient,
    AlpacaConnectionError,
    AlpacaOrderError,
)
from tradingagents.execution.alpaca_data import AlpacaDataClient
from tradingagents.execution.trade_db import TradeDB

print("All execution module imports successful")

# ----- TradeDB lifecycle test (in-memory) -----
import tempfile
import os

db_path = os.path.join(tempfile.mkdtemp(), "test.db")
db = TradeDB(db_path=db_path)
print(f"TradeDB created at {db_path}")

# Open position
db.open_position("NVDA", "2026-05-07", 125.0, 124.50, 100)
positions = db.get_open_positions()
assert len(positions) == 1, f"Expected 1 position, got {len(positions)}"
assert positions[0]["symbol"] == "NVDA"
assert positions[0]["entry_orl"] == 124.50
print(f"Open positions: {len(positions)} (NVDA, ORL=124.50)")

# Update position (Day 1 EOD: set LOD, increment day count)
db.update_position("NVDA", day_count=2, entry_lod=122.90)
pos = db.get_position("NVDA")
print(f"Updated: day_count={pos['day_count']}, entry_lod={pos['entry_lod']}")

# Record trim
db.update_position("NVDA", trimmed=1, trim_date="2026-05-09", current_qty=50)
pos = db.get_position("NVDA")
print(f"Trimmed: qty={pos['current_qty']}, trimmed={pos['trimmed']}")

# Close position
db.close_position("NVDA", "TRAIL_10SMA")
pos = db.get_position("NVDA")
print(f"Closed: status={pos['status']}, reason={pos['close_reason']}")

# Daily snapshot
db.save_daily_snapshot(
    "2026-05-07", 100000.0, 60000.0, 40000.0, 500.0, 0.005, 3, 2, 1
)
snap = db.get_daily_snapshot("2026-05-07")
print(f"Snapshot: portfolio={snap['portfolio_value']}, pnl={snap['daily_pnl']}")

# Order logging
db.log_order(
    "ord_123", "NVDA", "BUY", 100, "BRACKET", "FILLED",
    signal="Buy", entry_orl=124.50,
)
orders = db.get_orders_for_symbol("NVDA")
print(f"Orders for NVDA: {len(orders)}")

# Screening log
db.log_screening_result("2026-05-07", "NVDA", "alpaca_screener", 0.92, True, "Buy")
results = db.get_screening_results("2026-05-07")
print(f"Screening results: {len(results)}")

# Cleanup
os.remove(db_path)
print("\n=== All Phase 1 smoke tests PASSED ===")
