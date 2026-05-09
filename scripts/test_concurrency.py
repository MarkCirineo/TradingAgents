"""Test concurrent pipeline execution.

Runs 2-3 tickers through the LLM pipeline concurrently to verify:
1. ThreadPoolExecutor mechanics work
2. Thread-safety (memory log, config) holds
3. Wall-clock time shows actual parallelism

Does NOT execute any orders — analysis only.

Usage:
    .venv/Scripts/python scripts/test_concurrency.py
    .venv/Scripts/python scripts/test_concurrency.py --tickers AAPL NVDA TSLA
    .venv/Scripts/python scripts/test_concurrency.py --workers 2
"""

import argparse
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("test_concurrency")


def run_single_pipeline(symbol: str, trade_date: str) -> dict:
    """Run one pipeline and return timing + decision."""
    from tradingagents.graph.trading_graph import TradingAgentsGraph

    start = time.time()
    config = {
        "trading_mode": "daemon",
        # Use same LLM settings as daemon
    }

    # Merge with defaults
    from tradingagents.default_config import DEFAULT_CONFIG
    full_config = {**DEFAULT_CONFIG, **config}

    graph = TradingAgentsGraph(config=full_config)
    final_state, signal = graph.propagate(symbol, trade_date)
    elapsed = time.time() - start

    decision = final_state.get("final_trade_decision", "")[:200]
    return {
        "symbol": symbol,
        "elapsed": elapsed,
        "signal": signal,
        "decision_preview": decision,
    }


def main():
    parser = argparse.ArgumentParser(description="Test concurrent pipeline")
    parser.add_argument(
        "--tickers", nargs="+", default=["AAPL", "NVDA"],
        help="Tickers to analyze (default: AAPL NVDA)",
    )
    parser.add_argument(
        "--workers", type=int, default=2,
        help="Number of concurrent workers (default: 2)",
    )
    args = parser.parse_args()

    # Load env vars
    from dotenv import load_dotenv
    load_dotenv()

    trade_date = date.today().isoformat()
    tickers = args.tickers
    max_workers = args.workers

    print(f"\n{'='*60}")
    print(f"CONCURRENCY TEST")
    print(f"  Tickers:    {tickers}")
    print(f"  Workers:    {max_workers}")
    print(f"  Date:       {trade_date}")
    print(f"{'='*60}\n")

    # --- Run concurrently ---
    print(f"[1/2] Running {len(tickers)} tickers CONCURRENTLY (max_workers={max_workers})...")
    concurrent_start = time.time()
    results = {}

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {
            pool.submit(run_single_pipeline, ticker, trade_date): ticker
            for ticker in tickers
        }
        for future in as_completed(futures):
            ticker = futures[future]
            try:
                result = future.result()
                results[ticker] = result
                print(f"  {ticker}: done in {result['elapsed']:.0f}s — signal={result['signal']}")
            except Exception as exc:
                print(f"  {ticker}: FAILED — {exc}")
                results[ticker] = {"symbol": ticker, "elapsed": 0, "signal": "ERROR", "error": str(exc)}

    concurrent_elapsed = time.time() - concurrent_start

    # --- Summary ---
    print(f"\n{'='*60}")
    print(f"RESULTS")
    print(f"{'='*60}")

    individual_total = sum(r.get("elapsed", 0) for r in results.values())

    for ticker in tickers:
        r = results.get(ticker, {})
        if "error" in r:
            print(f"  {ticker}: ERROR — {r['error']}")
        else:
            print(f"  {ticker}: {r.get('elapsed', 0):.0f}s — {r.get('signal', 'N/A')}")
            print(f"    Decision: {r.get('decision_preview', 'N/A')[:100]}...")

    print(f"\n  Sum of individual times:  {individual_total:.0f}s")
    print(f"  Actual wall-clock time:   {concurrent_elapsed:.0f}s")
    if individual_total > 0:
        speedup = individual_total / concurrent_elapsed
        print(f"  Speedup:                  {speedup:.1f}x")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    main()
