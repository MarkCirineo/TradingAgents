"""Snapshot and equity curve API endpoints.

Serves historical portfolio data from the SQLite daily_snapshots
table for charting and analysis, plus an optional market benchmark
(SPY, or a SPY/cash blend) overlaid on the equity curve.
"""

from __future__ import annotations

import bisect
import logging
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter

logger = logging.getLogger(__name__)
router = APIRouter()

# Annual yield assumed for the cash sleeve of the blended benchmark —
# Alpaca's high-yield cash program (~3.3% APY as of 2026).
CASH_APY = 0.033


@router.get("/snapshots")
async def get_snapshots(days: int = 30):
    """Return recent daily portfolio snapshots.

    Used for the equity curve chart and historical analysis.
    """
    from tradingagents.dashboard.app import get_trade_db

    db = get_trade_db()
    snapshots = db.get_recent_snapshots(days=days)

    # Reverse to chronological order (DB returns most recent first)
    snapshots.reverse()

    return {"snapshots": snapshots, "count": len(snapshots)}


@router.get("/equity-curve")
async def get_equity_curve(days: int = 90, benchmark: str = "none"):
    """Return portfolio value time series formatted for charting.

    Returns data in TradingView Lightweight Charts format:
    ``[{time: "YYYY-MM-DD", value: 100000}, ...]``.

    When *benchmark* is ``"spy"`` or ``"blend"``, a second series is
    included, rebased to the account's equity at the start of the window
    so both lines share a dollar axis and start together:

    - ``spy``   — your starting equity invested fully in SPY.
    - ``blend`` — ``max_exposure_pct`` in SPY, the rest in cash compounding
      at ``CASH_APY`` (matches how the bot actually deploys capital).

    Also returns a ``summary`` with your return, the benchmark return, and
    the delta (alpha) over the visible window.
    """
    from tradingagents.dashboard.app import get_trade_db

    db = get_trade_db()
    snapshots = db.get_recent_snapshots(days=days)
    snapshots.reverse()  # chronological

    series = [
        {"time": s["date"], "value": s["portfolio_value"]}
        for s in snapshots
        if s.get("portfolio_value") is not None
    ]
    result = {"data": series, "count": len(series)}

    if benchmark in ("spy", "blend") and len(series) >= 2:
        try:
            bench = _benchmark_series(series, benchmark)
        except Exception as exc:  # never let a benchmark failure break the chart
            logger.warning("Benchmark (%s) computation failed: %s", benchmark, exc)
            bench = None
        if bench:
            result["benchmark"] = bench["series"]
            result["summary"] = bench["summary"]
            result["benchmark_type"] = benchmark

    return result


def _benchmark_series(series: list[dict], mode: str) -> Optional[dict]:
    """Build a SPY (or SPY/cash blend) series rebased to the account's
    starting equity, aligned to the equity-curve dates.

    Returns ``{"series": [...], "summary": {...}}`` or ``None`` if SPY data
    is unavailable.
    """
    from tradingagents.dashboard.app import get_config, get_data_client

    data_client = get_data_client()
    if data_client is None:
        return None

    anchor_date = series[0]["time"]
    last_date = series[-1]["time"]
    anchor_equity = float(series[0]["value"])
    if anchor_equity <= 0:
        return None

    anchor_dt = datetime.strptime(anchor_date, "%Y-%m-%d")
    last_dt = datetime.strptime(last_date, "%Y-%m-%d")

    # Fetch SPY daily closes covering the window (with a small lead buffer so
    # the anchor date itself has a bar even after weekends/holidays).
    bars = data_client.get_bars(
        "SPY",
        start=anchor_dt - timedelta(days=7),
        end=last_dt + timedelta(days=1),
    )
    if bars is None or bars.empty:
        return None

    import pandas as pd
    if isinstance(bars.index, pd.MultiIndex):
        bars = bars.xs("SPY", level="symbol")

    spy_by_date: dict[str, float] = {}
    for ts, row in bars.iterrows():
        try:
            spy_by_date[ts.strftime("%Y-%m-%d")] = float(row["close"])
        except Exception:
            continue
    if not spy_by_date:
        return None

    spy_dates = sorted(spy_by_date.keys())

    def aligned_close(target: str) -> Optional[float]:
        """Latest SPY close on or before *target* (carry-forward)."""
        i = bisect.bisect_right(spy_dates, target) - 1
        return spy_by_date[spy_dates[i]] if i >= 0 else None

    anchor_spy = aligned_close(anchor_date)
    if not anchor_spy:
        return None

    # Blend weights track the bot's actual max exposure (default 60/40).
    equity_weight = get_config().get("guardrails", {}).get("max_exposure_pct", 0.60)
    cash_weight = 1.0 - equity_weight

    bench = []
    for point in series:
        d = point["time"]
        spy_close = aligned_close(d)
        if not spy_close:
            continue
        spy_ratio = spy_close / anchor_spy
        if mode == "blend":
            days_elapsed = (datetime.strptime(d, "%Y-%m-%d") - anchor_dt).days
            cash_factor = (1.0 + CASH_APY) ** (max(days_elapsed, 0) / 365.0)
            value = anchor_equity * (equity_weight * spy_ratio + cash_weight * cash_factor)
        else:  # raw SPY
            value = anchor_equity * spy_ratio
        bench.append({"time": d, "value": round(value, 2)})

    if len(bench) < 2:
        return None

    your_return = float(series[-1]["value"]) / anchor_equity - 1.0
    bench_return = bench[-1]["value"] / bench[0]["value"] - 1.0
    summary = {
        "your_return": round(your_return, 4),
        "benchmark_return": round(bench_return, 4),
        "delta": round(your_return - bench_return, 4),
        "benchmark_type": mode,
    }
    return {"series": bench, "summary": summary}
