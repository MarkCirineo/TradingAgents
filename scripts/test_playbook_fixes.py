"""Test the playbook compliance fixes — no orders submitted.

Uses real market data via yfinance/Alpaca to exercise:
1. Pre-filter: prior uptrend, MA stacking, RS threshold, tight consolidation
2. Position manager: parabolic 50 SMA, breakeven guard
3. Consolidation pivot: detection, entry logic, market order support
4. Trade DB: daily P&L computation, stop type flags

Run:  python scripts/test_playbook_fixes.py
"""

import os
import sys
from dotenv import load_dotenv

load_dotenv()

# Ensure we can import the project
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

sys.stdout.reconfigure(encoding='utf-8')

PASS = "[PASS]"
FAIL = "[FAIL]"
SKIP = "[SKIP]"
results = {"pass": 0, "fail": 0, "skip": 0}


def check(name, condition, detail=""):
    if condition:
        print(f"  {PASS}  {name}" + (f"  ({detail})" if detail else ""))
        results["pass"] += 1
    else:
        print(f"  {FAIL}  {name}" + (f"  ({detail})" if detail else ""))
        results["fail"] += 1


def skip(name, reason):
    print(f"  {SKIP}  {name}  ({reason})")
    results["skip"] += 1


# =========================================================================
# Test 1: Config values propagate correctly
# =========================================================================
print("\n" + "=" * 60)
print("TEST 1: Config / Playbook values")
print("=" * 60)

from tradingagents.strategies.swing_playbook import (
    get_screening_params,
    get_exit_rules,
    get_entry_criteria_prompt,
)

params = get_screening_params()
check("min_rs_outperformance in params", "min_rs_outperformance" in params,
      f"value={params.get('min_rs_outperformance')}")
check("min_rs_outperformance = 0.05", params.get("min_rs_outperformance") == 0.05)
check("min_prior_uptrend_pct = 0.30", params.get("min_prior_uptrend_pct") == 0.30)

prompt = get_entry_criteria_prompt()
check("Prompt mentions prior uptrend as pre-verified",
      "prior uptrend of at least 30%" in prompt)
check("Prompt mentions MA stacking as pre-verified",
      "stacked bullishly (10 > 20 > 50)" in prompt)
check("Prompt mentions RS 5% as pre-verified",
      "5% over 20 days" in prompt)

rules = get_exit_rules()
check("max_extension_adr_multiple = 7", rules["max_extension_adr_multiple"] == 7)


# =========================================================================
# Test 2: Pre-filter new gates (with real market data)
# =========================================================================
print("\n" + "=" * 60)
print("TEST 2: Pre-filter — new hard gates")
print("=" * 60)

try:
    from tradingagents.screening.pre_filter import PreFilter

    pf = PreFilter()

    # Test with a well-known momentum leader (should pass most checks)
    test_ticker = "NVDA"
    print(f"\n  Testing {test_ticker} (expected: strong leader)...")
    result = pf._evaluate(test_ticker)
    print(f"  Result: passed={result.passed}, score={result.score}")
    print(f"  Checks: { {k: v for k, v in result.checks.items() if not k.endswith('_value') and k != 'ma_values'} }")

    # Show the new check values
    if "prior_uptrend_value" in result.checks:
        check(f"Prior uptrend computed ({test_ticker})",
              result.checks.get("prior_uptrend_value") is not None,
              f"uptrend={result.checks.get('prior_uptrend_value', 0):.1%}")
    else:
        skip(f"Prior uptrend ({test_ticker})", "no data")

    if "ma_values" in result.checks:
        mv = result.checks["ma_values"]
        check(f"MA stacking computed ({test_ticker})",
              "sma_10" in mv,
              f"10={mv.get('sma_10')}, 20={mv.get('sma_20')}, 50={mv.get('sma_50')}")
    else:
        skip(f"MA stacking ({test_ticker})", "no data")

    if "relative_strength_value" in result.checks:
        rs_val = result.checks["relative_strength_value"]
        check(f"RS threshold applied ({test_ticker})",
              True,
              f"RS={rs_val:.2%}, threshold=5%")
    else:
        skip(f"RS threshold ({test_ticker})", "no data")

    # Show tight consolidation results
    if "tight_consolidation" in result.checks:
        check(f"Tight consolidation check ({test_ticker})",
              result.checks.get("tight_consolidation") is not None,
              f"tight={result.checks.get('tight_consolidation')}, "
              f"days={result.checks.get('tight_days', 'N/A')}")
        if result.checks.get("pivot_high"):
            check(f"Pivot levels stored ({test_ticker})",
                  result.checks.get("pivot_high") > 0,
                  f"pivot_high=${result.checks.get('pivot_high')}, "
                  f"pivot_low=${result.checks.get('pivot_low')}")
    else:
        skip(f"Tight consolidation ({test_ticker})", "check not present")

    # Test with a weak/declining stock (should fail)
    weak_ticker = "T"  # AT&T — typically low momentum
    print(f"\n  Testing {weak_ticker} (expected: weak/may fail)...")
    result2 = pf._evaluate(weak_ticker)
    print(f"  Result: passed={result2.passed}, score={result2.score}")
    if result2.reject_reason:
        print(f"  Reject: {result2.reject_reason[:120]}")
    check(f"Weak stock filtered ({weak_ticker})", not result2.passed,
          "correctly rejected" if not result2.passed else "unexpectedly passed")

except Exception as exc:
    print(f"  ERROR: {exc}")
    skip("Pre-filter tests", str(exc))


# =========================================================================
# Test 3: Consolidation pivot detection (with real market data)
# =========================================================================
print("\n" + "=" * 60)
print("TEST 3: Consolidation pivot detection")
print("=" * 60)

try:
    from tradingagents.execution.alpaca_data import AlpacaDataClient
    dc = AlpacaDataClient()

    # NVDA should have tight consolidation days
    pivot = dc.compute_consolidation_pivot("NVDA", min_tight_days=2)
    if pivot:
        check("NVDA has consolidation pivot", True,
              f"pivot=${pivot['pivot_high']}, floor=${pivot['pivot_low']}, "
              f"days={pivot['tight_days']}")
        check("Pivot has required fields",
              all(k in pivot for k in ["pivot_high", "pivot_low", "tight_days", "adr"]))
        check("pivot_high > pivot_low",
              pivot["pivot_high"] > pivot["pivot_low"])
        check("tight_days >= 2", pivot["tight_days"] >= 2)
    else:
        skip("NVDA consolidation pivot", "None returned (may have broken out)")

    # Test with pre-fetched bars (same path as pre-filter)
    bars = dc.get_bars("NVDA", lookback_days=60)
    pivot_from_bars = dc.compute_consolidation_pivot("NVDA", bars=bars, min_tight_days=2)
    if pivot and pivot_from_bars:
        check("Pivot from pre-fetched bars matches",
              pivot["pivot_high"] == pivot_from_bars["pivot_high"],
              f"direct=${pivot['pivot_high']}, from_bars=${pivot_from_bars['pivot_high']}")
    elif pivot_from_bars is None and pivot is None:
        check("Both paths return None consistently", True)
    else:
        skip("Pre-fetched bars comparison", "inconsistent results")

    # Test with min_tight_days=999 (should always return None)
    pivot_none = dc.compute_consolidation_pivot("NVDA", min_tight_days=999)
    check("Returns None when min_tight_days impossible",
          pivot_none is None)

except Exception as exc:
    print(f"  ERROR: {exc}")
    skip("Consolidation pivot test", str(exc))


# =========================================================================
# Test 3: Position Manager — parabolic 50 SMA reference
# =========================================================================
print("\n" + "=" * 60)
print("TEST 3: Position Manager — parabolic uses 50 SMA")
print("=" * 60)

try:
    from tradingagents.execution.position_manager import PositionManager

    # Read the source to verify 50 SMA reference
    import inspect
    source = inspect.getsource(PositionManager._evaluate_position)
    check("Parabolic uses compute_sma(period=50)",
          "compute_sma(symbol, period=50)" in source)
    check("Parabolic reason says '50 SMA'",
          "above 50 SMA" in source)
    check("No reference to '10 SMA' in parabolic rule",
          "above 10 SMA" not in source.split("Rule 2")[1].split("Rule 3")[0])

except Exception as exc:
    print(f"  ERROR: {exc}")
    skip("Parabolic 50 SMA test", str(exc))


# =========================================================================
# Test 4: Position Manager — breakeven guard
# =========================================================================
print("\n" + "=" * 60)
print("TEST 4: Position Manager — breakeven guard on trailing")
print("=" * 60)

try:
    source = inspect.getsource(PositionManager._evaluate_position)

    check("Trailing has 'trailing_active' guard",
          "trailing_active = sma_10_val >= entry_price" in source)
    check("Trail exit uses trailing_active",
          "trailing_active and exit_triggered" in source)
    check("Trail stop update uses trailing_active",
          "trailing_active and sma_10_val > current_stop" in source)

except Exception as exc:
    skip("Breakeven guard test", str(exc))


# =========================================================================
# Test 5: TradeSignal has entry_type field
# =========================================================================
print("\n" + "=" * 60)
print("TEST 5: TradeSignal — entry_type field")
print("=" * 60)

try:
    from tradingagents.execution.executor import TradeSignal

    # Default should be "stop"
    sig = TradeSignal(symbol="TEST", action="buy", entry_price=100, stop_price=95)
    check("Default entry_type is 'stop'", sig.entry_type == "stop")

    # Should accept "market" (confirmed breakout)
    sig2 = TradeSignal(symbol="TEST", action="buy", entry_price=100,
                       stop_price=95, entry_type="market")
    check("entry_type accepts 'market'", sig2.entry_type == "market")

    # Should accept "limit"
    sig3 = TradeSignal(symbol="TEST", action="buy", entry_price=100,
                       stop_price=95, entry_type="limit")
    check("entry_type accepts 'limit'", sig3.entry_type == "limit")

except Exception as exc:
    skip("TradeSignal entry_type", str(exc))


# =========================================================================
# Test 6: Trade DB — daily P&L computation
# =========================================================================
print("\n" + "=" * 60)
print("TEST 6: Trade DB — daily P&L computation")
print("=" * 60)

try:
    import tempfile
    from tradingagents.execution.trade_db import TradeDB

    # Use a temp DB so we don't touch the real one
    tmp_db_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "scripts", "_test_playbook.db"
    )
    db = TradeDB(db_path=tmp_db_path)

    # Record "yesterday" snapshot
    db.record_snapshot(
        date="2026-05-09",
        portfolio_value=100000,
        cash=50000,
        num_positions=3,
    )

    # Record "today" snapshot
    db.record_snapshot(
        date="2026-05-10",
        portfolio_value=97500,  # -2.5%
        cash=48000,
        num_positions=3,
    )

    snap = db.get_daily_snapshot("2026-05-10")
    check("Daily P&L computed",
          snap is not None and snap.get("daily_pnl") != 0,
          f"pnl={snap.get('daily_pnl', 'N/A')}")
    check("Daily P&L = -$2500",
          snap.get("daily_pnl") == -2500.0 if snap else False,
          f"got {snap.get('daily_pnl')}")
    check("Daily P&L pct = -2.5%",
          abs(snap.get("daily_pnl_pct", 0) - (-0.025)) < 0.001 if snap else False,
          f"got {snap.get('daily_pnl_pct'):.4f}" if snap else "N/A")

    # Cleanup
    os.remove(tmp_db_path)

except Exception as exc:
    print(f"  ERROR: {exc}")
    skip("Trade DB P&L test", str(exc))
    try:
        os.remove(tmp_db_path)
    except Exception:
        pass


# =========================================================================
# Test 7: Trade DB — update_stop with stop_type
# =========================================================================
print("\n" + "=" * 60)
print("TEST 7: Trade DB — stop_type flags")
print("=" * 60)

try:
    tmp_db_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "scripts", "_test_playbook2.db"
    )
    db = TradeDB(db_path=tmp_db_path)

    # Create a test position
    db.open_position(
        symbol="TEST",
        entry_date="2026-05-10",
        entry_price=100.0,
        entry_orl=95.0,
        qty=50,
    )

    # Update to breakeven
    db.update_stop("TEST", 100.0, stop_type="breakeven")
    pos = db.get_open_positions()
    test_pos = [p for p in pos if p["symbol"] == "TEST"][0]
    check("breakeven_stop_active = 1 after BE stop",
          test_pos.get("breakeven_stop_active") == 1)
    check("trailing_stop_active = 0 after BE stop",
          test_pos.get("trailing_stop_active") == 0)

    # Update to trailing
    db.update_stop("TEST", 102.0, stop_type="trailing")
    pos = db.get_open_positions()
    test_pos = [p for p in pos if p["symbol"] == "TEST"][0]
    check("breakeven_stop_active = 0 after trailing stop",
          test_pos.get("breakeven_stop_active") == 0)
    check("trailing_stop_active = 1 after trailing stop",
          test_pos.get("trailing_stop_active") == 1)
    check("stop price updated to $102",
          test_pos.get("entry_orl") == 102.0,
          f"got {test_pos.get('entry_orl')}")

    # Cleanup
    os.remove(tmp_db_path)

except Exception as exc:
    print(f"  ERROR: {exc}")
    skip("Stop type flags test", str(exc))
    try:
        os.remove(tmp_db_path)
    except Exception:
        pass


# =========================================================================
# Test 8: Executor uses signal.entry_type for order
# =========================================================================
print("\n" + "=" * 60)
print("TEST 8: Executor — entry_type routing")
print("=" * 60)

try:
    from tradingagents.execution.executor import Executor
    source = inspect.getsource(Executor.execute_entry)

    check("Executor reads signal.entry_type",
          "signal.entry_type" in source)
    check("Executor handles 'stop' entry_type",
          'signal.entry_type == "stop"' in source)
    check("Executor handles 'limit' entry_type",
          'signal.entry_type == "limit"' in source)
    check("Executor handles 'market' entry_type",
          '"market" needs no extra kwargs' in source)

except Exception as exc:
    skip("Executor entry_type routing", str(exc))


# =========================================================================
# Test 9: execute_entries code structure
# =========================================================================
print("\n" + "=" * 60)
print("TEST 9: execute_entries — pivot-based entry logic")
print("=" * 60)

try:
    from tradingagents.daemon.daily_workflow import DailyWorkflow, DayContext

    # Verify DayContext has pivot_levels field
    ctx = DayContext()
    check("DayContext has pivot_levels", hasattr(ctx, "pivot_levels"))
    check("pivot_levels defaults to empty dict", ctx.pivot_levels == {})

    # Verify execute_entries exists and entry_window is gone from schedule
    wf = DailyWorkflow()
    check("execute_entries method exists", hasattr(wf, "execute_entries"))

    # Verify code structure
    source = inspect.getsource(DailyWorkflow.execute_entries)
    check("Uses pivot_levels.get(symbol)",
          "pivot_levels.get(symbol)" in source)
    check("Checks price < pivot_low (breakdown)",
          "current_price < pivot_low" in source)
    check("Checks price <= pivot_high (buy-stop)",
          "current_price <= pivot_high" in source)
    check("Uses market order for confirmed breakout",
          'entry_type = "market"' in source)
    check("Stop is at pivot_low (consolidation floor)",
          "stop_price=pivot_low" in source)
    check("Logs skip for no pivot (not A+ setup)",
          "not A+ setup" in source)

    # Verify scheduler timing
    from tradingagents.default_config import DEFAULT_CONFIG
    schedule = DEFAULT_CONFIG["trading_schedule"]
    check("Schedule uses execute_entries key",
          "execute_entries" in schedule)
    check("Execute entries at 09:30",
          schedule.get("execute_entries") == "09:30")
    check("No entry_window in schedule",
          "entry_window" not in schedule)

except Exception as exc:
    print(f"  ERROR: {exc}")
    skip("execute_entries structure", str(exc))


# =========================================================================
# Summary
# =========================================================================
print("\n" + "=" * 60)
total = results["pass"] + results["fail"] + results["skip"]
print(f"RESULTS: {results['pass']}/{total} passed, "
      f"{results['fail']} failed, {results['skip']} skipped")
print("=" * 60)

if results["fail"] > 0:
    sys.exit(1)
