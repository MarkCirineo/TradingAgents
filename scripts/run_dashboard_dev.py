"""Run the dashboard locally on port 8055 for UI development.

Uses the default local DB (~/.tradingagents/trades.db) and whatever
Alpaca keys are in .env — NOT the Docker services' DB/account.  Safe to
run alongside the live services (different port, read-only against
Alpaca).
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DASHBOARD_PORT", "8055")
os.environ.setdefault("DASHBOARD_HOST", "127.0.0.1")

from tradingagents.dashboard.app import main

main()
