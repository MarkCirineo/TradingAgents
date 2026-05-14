"""Quantitative pre-filter: hard-coded screening criteria from the swing playbook.

Every check in this module is a deterministic, numeric gate -- the LLM never
sees tickers that fail these filters.  This is where the doc's quantitative
wisdom lives in code:

- Dollar volume > $50M
- ADR > 4%
- Price $5-$500
- Market regime (SPY above rising 20 MA, 10 MA > 20 MA)
- Relative strength (outperforming SPY by >= 5% over 20 days)
- Prior uptrend (>= 30% gain over 60 days)
- MA stacking (10 SMA > 20 SMA > 50 SMA -- bullish stack)
- Volume contraction (consolidation signal)
- Tight consolidation (>= 2 days with range <= 2/3 ADR; provides pivot levels)
- Already-held filter (skip if we have an open position)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from tradingagents.strategies.swing_playbook import get_screening_params

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Filter result
# ---------------------------------------------------------------------------

@dataclass
class FilterResult:
    """Result of running a candidate through the pre-filter."""

    symbol: str
    passed: bool
    score: float = 0.0
    checks: dict = None  # individual check results
    reject_reason: str = ""

    def __post_init__(self):
        if self.checks is None:
            self.checks = {}


# ---------------------------------------------------------------------------
# Pre-filter engine
# ---------------------------------------------------------------------------

class PreFilter:
    """Apply quantitative screening criteria to ticker candidates.

    Parameters
    ----------
    data_client : AlpacaDataClient, optional
        Market data client for fetching bars and indicators.
    trade_db : TradeDB, optional
        Trade database for checking existing positions.
    config : dict, optional
        Configuration dictionary.
    """

    def __init__(
        self,
        data_client=None,
        trade_db=None,
        config: Optional[Dict[str, Any]] = None,
    ):
        self._data_client = data_client
        self._trade_db = trade_db
        self._params = get_screening_params(config)

    @property
    def data_client(self):
        if self._data_client is None:
            from tradingagents.execution.alpaca_data import AlpacaDataClient
            self._data_client = AlpacaDataClient()
        return self._data_client

    # -- public API ---------------------------------------------------------

    def filter_candidates(self, symbols: List[str]) -> List[FilterResult]:
        """Run all filters on each symbol and return results.

        Returns a list of ``FilterResult`` objects, sorted by score
        descending.  Only passing candidates are included.
        """
        results = []
        for symbol in symbols:
            result = self._evaluate(symbol)
            if result.passed:
                results.append(result)
            else:
                logger.info(
                    "Pre-filter REJECTED %s: %s", symbol, result.reject_reason
                )
        results.sort(key=lambda r: r.score, reverse=True)
        logger.info(
            "Pre-filter: %d/%d candidates passed", len(results), len(symbols)
        )
        return results

    # -- individual checks --------------------------------------------------

    def _evaluate(self, symbol: str) -> FilterResult:
        """Run all checks on a single symbol."""
        checks = {}
        reject_reasons = []
        score = 0.0

        # 1. Already held?
        if self._trade_db:
            positions = self._trade_db.get_open_positions()
            held_symbols = {p["symbol"] for p in positions}
            already_held = symbol in held_symbols
            checks["already_held"] = not already_held
            if already_held:
                reject_reasons.append("already holding position")
        else:
            checks["already_held"] = True  # no DB = skip check

        # 2. Dollar volume
        try:
            dollar_vol = self.data_client.get_dollar_volume(symbol, period=20)
            checks["dollar_volume"] = dollar_vol >= self._params["min_dollar_volume"]
            checks["dollar_volume_value"] = dollar_vol
            if checks["dollar_volume"]:
                score += 1.0
            else:
                reject_reasons.append(
                    f"dollar volume ${dollar_vol:,.0f} < ${self._params['min_dollar_volume']:,.0f}"
                )
        except Exception as exc:
            logger.warning("Dollar volume check failed for %s: %s", symbol, exc)
            checks["dollar_volume"] = False
            reject_reasons.append(f"dollar volume check error: {exc}")

        # 3. ADR%
        try:
            adr_pct = self.data_client.compute_adr_pct(symbol, period=14)
            checks["adr_pct"] = adr_pct >= self._params["min_adr_pct"]
            checks["adr_pct_value"] = round(adr_pct, 4)
            if checks["adr_pct"]:
                score += 1.0
            else:
                reject_reasons.append(
                    f"ADR {adr_pct:.2%} < {self._params['min_adr_pct']:.0%}"
                )
        except Exception as exc:
            logger.warning("ADR check failed for %s: %s", symbol, exc)
            checks["adr_pct"] = False
            reject_reasons.append(f"ADR check error: {exc}")

        # 4. Price range
        try:
            bars = self.data_client.get_bars(symbol, lookback_days=5)
            if not bars.empty:
                import pandas as pd
                if isinstance(bars.index, pd.MultiIndex):
                    bars = bars.xs(symbol, level="symbol")
                latest_close = float(bars["close"].iloc[-1])
                in_range = self._params["min_price"] <= latest_close <= self._params["max_price"]
                checks["price_range"] = in_range
                checks["price"] = latest_close
                if in_range:
                    score += 0.5
                else:
                    reject_reasons.append(
                        f"price ${latest_close:.2f} outside ${self._params['min_price']}-${self._params['max_price']}"
                    )
            else:
                checks["price_range"] = False
                reject_reasons.append("no price data available")
        except Exception as exc:
            logger.warning("Price check failed for %s: %s", symbol, exc)
            checks["price_range"] = False
            reject_reasons.append(f"price check error: {exc}")

        # 5. Relative strength vs SPY
        try:
            rs = self.data_client.compute_relative_strength(symbol, benchmark="SPY", period=20)
            min_rs = self._params.get("min_rs_outperformance", 0.05)
            checks["relative_strength"] = rs >= min_rs  # must outperform by >= 5%
            checks["relative_strength_value"] = round(rs, 4)
            if checks["relative_strength"]:
                score += 1.5  # High weight -- this is key for the strategy
            else:
                reject_reasons.append(
                    f"RS {rs:.2%} < required {min_rs:.0%} above SPY"
                )
        except Exception as exc:
            logger.warning("RS check failed for %s: %s", symbol, exc)
            checks["relative_strength"] = False
            reject_reasons.append(f"RS check error: {exc}")

        # 6. Volume contraction (consolidation signal)
        try:
            bars_60d = self.data_client.get_bars(symbol, lookback_days=60)
            if not bars_60d.empty:
                import pandas as pd
                if isinstance(bars_60d.index, pd.MultiIndex):
                    bars_60d = bars_60d.xs(symbol, level="symbol")
                vol = bars_60d["volume"]
                recent_5d = vol.tail(5).mean()
                avg_20d = vol.tail(20).mean()
                contracting = recent_5d < avg_20d
                checks["volume_contraction"] = contracting
                checks["vol_ratio"] = round(recent_5d / avg_20d, 3) if avg_20d > 0 else 0
                if contracting:
                    score += 0.5  # Bonus for consolidation pattern
            else:
                checks["volume_contraction"] = False
        except Exception as exc:
            logger.warning("Volume contraction check failed for %s: %s", symbol, exc)
            checks["volume_contraction"] = False

        # 7. Prior uptrend check (doc: "30%+ move in recent weeks/months")
        try:
            bars_uptrend = self.data_client.get_bars(symbol, lookback_days=90)
            if not bars_uptrend.empty:
                import pandas as pd
                if isinstance(bars_uptrend.index, pd.MultiIndex):
                    bars_uptrend = bars_uptrend.xs(symbol, level="symbol")
                if len(bars_uptrend) >= 40:
                    close = bars_uptrend["close"]
                    # Find the max return from any point in the last 60 trading
                    # days to the current close — this captures the "pole" move.
                    current_close = float(close.iloc[-1])
                    lookback = close.tail(60)
                    min_price_in_window = float(lookback.min())
                    uptrend_pct = (current_close - min_price_in_window) / min_price_in_window
                    min_uptrend = self._params.get("min_prior_uptrend_pct", 0.30)
                    checks["prior_uptrend"] = uptrend_pct >= min_uptrend
                    checks["prior_uptrend_value"] = round(uptrend_pct, 4)
                    if checks["prior_uptrend"]:
                        score += 1.0
                    else:
                        reject_reasons.append(
                            f"prior uptrend {uptrend_pct:.1%} < required {min_uptrend:.0%}"
                        )
                else:
                    checks["prior_uptrend"] = False
                    reject_reasons.append("insufficient data for uptrend check")
            else:
                checks["prior_uptrend"] = False
                reject_reasons.append("no data for uptrend check")
        except Exception as exc:
            logger.warning("Prior uptrend check failed for %s: %s", symbol, exc)
            checks["prior_uptrend"] = False
            reject_reasons.append(f"uptrend check error: {exc}")

        # 8. MA stacking (doc: "10 > 20 > 50 -- bullish stack")
        try:
            sma_10 = self.data_client.compute_sma(symbol, period=10, lookback_days=100)
            sma_20 = self.data_client.compute_sma(symbol, period=20, lookback_days=100)
            sma_50 = self.data_client.compute_sma(symbol, period=50, lookback_days=100)
            if not sma_10.empty and not sma_20.empty and not sma_50.empty:
                s10 = float(sma_10.iloc[-1])
                s20 = float(sma_20.iloc[-1])
                s50 = float(sma_50.iloc[-1])
                stacked = s10 > s20 > s50
                checks["ma_stacking"] = stacked
                checks["ma_values"] = {"sma_10": round(s10, 2), "sma_20": round(s20, 2), "sma_50": round(s50, 2)}
                if stacked:
                    score += 1.0
                else:
                    reject_reasons.append(
                        f"MAs not stacked: 10={s10:.2f}, 20={s20:.2f}, 50={s50:.2f}"
                    )
            else:
                checks["ma_stacking"] = False
                reject_reasons.append("insufficient data for MA stacking")
        except Exception as exc:
            logger.warning("MA stacking check failed for %s: %s", symbol, exc)
            checks["ma_stacking"] = False
            reject_reasons.append(f"MA stacking error: {exc}")

        # 9. Tight consolidation (doc: "Daily Range <= 2/3 * ADR", min 2 days)
        #    Uses bars_60d already fetched in check #6 — zero extra API calls.
        #    Provides pivot_high (entry trigger) and pivot_low (stop level).
        try:
            pivot = self.data_client.compute_consolidation_pivot(
                symbol, bars=bars_60d, min_tight_days=2,
            )
            if pivot:
                checks["tight_consolidation"] = True
                checks["pivot_high"] = pivot["pivot_high"]
                checks["pivot_low"] = pivot["pivot_low"]
                checks["tight_days"] = pivot["tight_days"]
                score += 1.0
            else:
                checks["tight_consolidation"] = False
                reject_reasons.append(
                    "no tight consolidation (< 2 days with range <= 2/3 ADR)"
                )
        except Exception as exc:
            logger.warning("Tight consolidation check failed for %s: %s", symbol, exc)
            checks["tight_consolidation"] = False
            reject_reasons.append(f"consolidation check error: {exc}")

        # Determine pass/fail
        # Must pass: not already held, dollar volume, ADR, price range,
        # relative strength, prior uptrend, MA stacking, tight consolidation
        required_checks = [
            checks.get("already_held", False),
            checks.get("dollar_volume", False),
            checks.get("adr_pct", False),
            checks.get("price_range", False),
            checks.get("relative_strength", False),
            checks.get("prior_uptrend", False),
            checks.get("ma_stacking", False),
            checks.get("tight_consolidation", False),
        ]
        passed = all(required_checks)

        return FilterResult(
            symbol=symbol,
            passed=passed,
            score=round(score, 3),
            checks=checks,
            reject_reason="; ".join(reject_reasons) if reject_reasons else "",
        )


# ---------------------------------------------------------------------------
# Market regime check (run ONCE before any individual stock filtering)
# ---------------------------------------------------------------------------

def check_market_regime(data_client=None, config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Check whether the overall market regime is favorable for breakouts.

    This runs ONCE before the pre-filter loop.  If the regime is
    unfavorable, we skip all new entries for the day.

    Returns
    -------
    dict
        ``favorable`` (bool), plus detail fields for logging.
    """
    params = get_screening_params(config)

    if data_client is None:
        from tradingagents.execution.alpaca_data import AlpacaDataClient
        data_client = AlpacaDataClient()

    result = {"favorable": True, "checks": {}}

    # 1. SPY moving average check
    try:
        spy_sma_10 = data_client.compute_sma("SPY", period=10)
        spy_sma_20 = data_client.compute_sma("SPY", period=20)

        if spy_sma_10.empty or spy_sma_20.empty:
            logger.warning("Could not compute SPY MAs -- skipping regime check")
            result["checks"]["spy_ma"] = "data unavailable"
            return result

        latest_close_bars = data_client.get_bars("SPY", lookback_days=5)
        import pandas as pd
        if isinstance(latest_close_bars.index, pd.MultiIndex):
            latest_close_bars = latest_close_bars.xs("SPY", level="symbol")
        spy_close = float(latest_close_bars["close"].iloc[-1])

        spy_10 = float(spy_sma_10.iloc[-1])
        spy_20 = float(spy_sma_20.iloc[-1])

        above_20 = spy_close > spy_20
        ma_stacked = spy_10 > spy_20

        result["checks"]["spy_close"] = spy_close
        result["checks"]["spy_10_sma"] = round(spy_10, 2)
        result["checks"]["spy_20_sma"] = round(spy_20, 2)
        result["checks"]["spy_above_20ma"] = above_20
        result["checks"]["spy_10_above_20"] = ma_stacked

        if not above_20 or not ma_stacked:
            result["favorable"] = False
            logger.warning(
                "Market regime UNFAVORABLE: SPY=%.2f, 10MA=%.2f, 20MA=%.2f "
                "(above_20=%s, stacked=%s)",
                spy_close, spy_10, spy_20, above_20, ma_stacked,
            )
    except Exception as exc:
        logger.error("SPY regime check failed: %s", exc)
        result["checks"]["spy_ma"] = f"error: {exc}"

    # 2. VIX regime adjustments -- modulates sizing, not a binary gate.
    #    SPY MA stacking above is the primary gate (per Qullamaggie).
    #    VIX adjusts HOW MUCH we trade, not WHETHER we trade.
    try:
        import yfinance as yf
        from tradingagents.strategies.swing_playbook import get_regime_adjustments

        vix = yf.Ticker("^VIX")
        vix_data = vix.history(period="1d")
        if not vix_data.empty:
            vix_level = float(vix_data["Close"].iloc[-1])
            regime = get_regime_adjustments(vix_level)
            result["checks"]["vix_level"] = vix_level
            result["regime"] = regime
            logger.info(
                "VIX=%.2f -> Regime: %s (risk=%.2f%%, max_pos=%d, exposure=%.0f%%)",
                vix_level,
                regime["label"],
                regime["risk_pct"] * 100,
                regime["max_positions"],
                regime["max_exposure_pct"] * 100,
            )

            # VIX > 30 pauses new entries (breakouts fail in panic)
            if regime["pause_entries"]:
                result["favorable"] = False
                logger.warning(
                    "VIX=%.2f PANIC regime: pausing new breakout entries "
                    "(existing positions still managed)",
                    vix_level,
                )
        else:
            # No VIX data -- use normal defaults
            result["regime"] = get_regime_adjustments(17.0)  # assume normal
    except Exception as exc:
        logger.debug("VIX regime fetch failed (using defaults): %s", exc)
        from tradingagents.strategies.swing_playbook import get_regime_adjustments
        result["regime"] = get_regime_adjustments(17.0)  # assume normal

    return result


