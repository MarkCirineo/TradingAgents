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

All checks for a symbol compute off a single ~100-day daily-bar frame.
``evaluate_all``/``filter_candidates`` fetch bars for every candidate
(plus SPY) in one batched multi-symbol request -- a 35-symbol run costs
a handful of API calls instead of ~9 per symbol.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import pandas as pd

from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.strategies.swing_playbook import (
    calculate_shares,
    get_screening_params,
    get_sizing_params,
)

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
        portfolio_value: Optional[float] = None,
        risk_pct: Optional[float] = None,
    ):
        self._data_client = data_client
        self._trade_db = trade_db
        self._params = get_screening_params(config)
        self._sizing = get_sizing_params(config)
        # Account equity for the exact whole-share gate.  When known (and
        # dynamic sizing is on), candidates whose real pivot-based stop would
        # size to 0 shares are filtered here instead of silently dropping at
        # entry.  When None, the gate is skipped (behaviour unchanged).
        self._portfolio_value = portfolio_value
        # Risk-per-trade to size with -- must match the executor's, which is
        # VIX-regime-adjusted (calm bumps it above base, elevated cuts it).
        # Using the base here would desync the gate from live sizing and could
        # over-filter in a calm regime.  Falls back to the static target.
        self._risk_pct = (
            risk_pct if risk_pct is not None else self._sizing["target_risk_pct"]
        )
        self._dynamic_sizing = (config or DEFAULT_CONFIG).get(
            "swing_strategy", {}
        ).get("dynamic_max_price", True)

    @property
    def data_client(self):
        if self._data_client is None:
            from tradingagents.execution.alpaca_data import AlpacaDataClient
            self._data_client = AlpacaDataClient()
        return self._data_client

    # How much history each symbol needs: 50-SMA and the 60-trading-day
    # uptrend window both fit comfortably in ~100 calendar days of bars.
    _LOOKBACK_DAYS = 100
    # Symbols per multi-symbol bars request (stay well under URL limits).
    _BATCH_CHUNK = 100

    # -- public API ---------------------------------------------------------

    def filter_candidates(self, symbols: List[str]) -> List[FilterResult]:
        """Run all filters on each symbol and return results.

        Returns a list of ``FilterResult`` objects, sorted by score
        descending.  Only passing candidates are included.
        """
        results = []
        for result in self.evaluate_all(symbols):
            if result.passed:
                results.append(result)
            else:
                logger.info(
                    "Pre-filter REJECTED %s: %s", result.symbol, result.reject_reason
                )
        results.sort(key=lambda r: r.score, reverse=True)
        logger.info(
            "Pre-filter: %d/%d candidates passed", len(results), len(symbols)
        )
        return results

    def evaluate_all(self, symbols: List[str]) -> List[FilterResult]:
        """Evaluate every symbol and return ALL results (passing or not).

        Daily bars for all symbols plus SPY are fetched in one batched
        request; each symbol's checks then run off its slice.
        """
        bars_map = self._fetch_bars_batch(symbols)
        spy_bars = bars_map.get("SPY")
        if spy_bars is None:
            # Batch failed or SPY missing -- fetch once, not per symbol.
            spy_bars = self._fetch_bars_single("SPY")
        return [
            self._evaluate(symbol, bars=bars_map.get(symbol), spy_bars=spy_bars)
            for symbol in symbols
        ]

    # -- bar fetching --------------------------------------------------------

    def _fetch_bars_batch(self, symbols: List[str]) -> Dict[str, pd.DataFrame]:
        """Fetch ~100 days of daily bars for all *symbols* + SPY, batched.

        Returns a mapping of ``symbol -> DataFrame``.  Symbols with no
        data are simply absent from the result.
        """
        all_symbols = list(dict.fromkeys(list(symbols) + ["SPY"]))
        frames: Dict[str, pd.DataFrame] = {}
        for i in range(0, len(all_symbols), self._BATCH_CHUNK):
            chunk = all_symbols[i : i + self._BATCH_CHUNK]
            try:
                df = self.data_client.get_bars(
                    chunk, lookback_days=self._LOOKBACK_DAYS
                )
            except Exception as exc:
                logger.warning(
                    "Batch bars fetch failed for %d symbols (%s) -- "
                    "falling back to per-symbol fetches", len(chunk), exc,
                )
                continue
            if df.empty:
                continue
            if isinstance(df.index, pd.MultiIndex):
                for sym in df.index.get_level_values("symbol").unique():
                    frames[sym] = df.xs(sym, level="symbol")
            elif len(chunk) == 1:
                frames[chunk[0]] = df
        return frames

    def _fetch_bars_single(self, symbol: str) -> pd.DataFrame:
        """Fetch daily bars for one symbol (fallback path)."""
        try:
            df = self.data_client.get_bars(
                symbol, lookback_days=self._LOOKBACK_DAYS
            )
        except Exception as exc:
            logger.warning("Bars fetch failed for %s: %s", symbol, exc)
            return pd.DataFrame()
        if df.empty:
            return df
        if isinstance(df.index, pd.MultiIndex):
            try:
                df = df.xs(symbol, level="symbol")
            except KeyError:
                return pd.DataFrame()
        return df

    @staticmethod
    def _relative_strength(
        bars: Optional[pd.DataFrame],
        spy_bars: Optional[pd.DataFrame],
        period: int = 20,
    ) -> float:
        """Return symbol_return - SPY_return over *period* trading days.

        Returns 0.0 when either side has insufficient data (same
        behavior as the old per-symbol fetch path).
        """
        if bars is None or spy_bars is None:
            return 0.0
        if len(bars) < period or len(spy_bars) < period:
            return 0.0
        returns = []
        for df in (bars, spy_bars):
            close = df["close"]
            returns.append(
                float((close.iloc[-1] - close.iloc[-period]) / close.iloc[-period])
            )
        return returns[0] - returns[1]

    # -- quality ranking -----------------------------------------------------

    @staticmethod
    def compute_quality_score(checks: dict) -> float:
        """Compute a continuous 0-100 quality score from pre-filter check data.

        Uses 6 dimensions weighted by importance per the strategy doc:
          - Relative Strength   (30%)  "You need to be in the best stocks"
          - Consolidation Tight (25%)  "Tighter the better"
          - Prior Uptrend       (20%)  "Bigger first leg, bigger second leg"
          - ADR%                (10%)  "High ADR is equal to gold"
          - Pivot Proximity     (10%)  "Do not chase"
          - Dollar Volume        (5%)  Liquidity floor

        Each dimension is normalized to 0.0-1.0 via min-max scaling
        against sensible bounds, then weighted and summed to 0-100.

        This score does NOT affect pass/fail — it only determines the
        processing order so the best setups get first dibs on limited
        position slots.
        """

        def _clamp(value: float, lo: float = 0.0, hi: float = 1.0) -> float:
            return max(lo, min(hi, value))

        # --- 1. Relative Strength (30%) ---
        # 5% outperformance = 0.0 (bare minimum to pass), 30%+ = 1.0
        rs_val = checks.get("relative_strength_value", 0.05)
        rs_score = _clamp((rs_val - 0.05) / (0.30 - 0.05))

        # --- 2. Consolidation Tightness (25%) ---
        # Combines volume contraction ratio (lower = better, 60% weight)
        # and tight days count (more = better, 40% weight)
        vol_ratio = checks.get("vol_ratio", 1.0)
        vol_component = _clamp((1.0 - vol_ratio) / (1.0 - 0.40))
        tight_days = checks.get("tight_days", 2)
        days_component = _clamp((tight_days - 2) / (7 - 2))
        tightness_score = 0.6 * vol_component + 0.4 * days_component

        # --- 3. Prior Uptrend Strength (20%) ---
        # 30% gain = 0.0 (bare minimum), 100%+ = 1.0
        uptrend_val = checks.get("prior_uptrend_value", 0.30)
        uptrend_score = _clamp((uptrend_val - 0.30) / (1.00 - 0.30))

        # --- 4. ADR% (10%) ---
        # 4% = 0.0 (bare minimum), 12%+ = 1.0
        adr_val = checks.get("adr_pct_value", 0.04)
        adr_score = _clamp((adr_val - 0.04) / (0.12 - 0.04))

        # --- 5. Pivot Proximity (10%) ---
        # Below or at pivot = 1.0 (best: buy-stop territory)
        # Above pivot, decays linearly to 0.0 at 1R of extension
        price = checks.get("price", 0)
        pivot_high = checks.get("pivot_high", 0)
        pivot_low = checks.get("pivot_low", 0)
        risk_per_share = pivot_high - pivot_low if pivot_high and pivot_low else 0
        if price <= 0 or pivot_high <= 0 or risk_per_share <= 0:
            proximity_score = 0.5  # neutral fallback if data missing
        elif price <= pivot_high:
            proximity_score = 1.0  # still in consolidation — ideal
        else:
            proximity_score = _clamp(
                1.0 - (price - pivot_high) / risk_per_share
            )

        # --- 6. Dollar Volume (5%) ---
        # $50M = 0.0 (bare minimum), $500M+ = 1.0
        dvol = checks.get("dollar_volume_value", 50_000_000)
        dvol_score = _clamp((dvol - 50_000_000) / (500_000_000 - 50_000_000))

        # --- Weighted composite ---
        quality = (
            rs_score * 0.30
            + tightness_score * 0.25
            + uptrend_score * 0.20
            + adr_score * 0.10
            + proximity_score * 0.10
            + dvol_score * 0.05
        ) * 100

        return round(quality, 1)

    # -- individual checks --------------------------------------------------

    def _evaluate(
        self,
        symbol: str,
        bars: Optional[pd.DataFrame] = None,
        spy_bars: Optional[pd.DataFrame] = None,
    ) -> FilterResult:
        """Run all checks on a single symbol off one daily-bar frame.

        Parameters
        ----------
        bars : pd.DataFrame, optional
            Pre-fetched ~100-day daily bars for *symbol*.  When omitted,
            fetched individually -- prefer ``evaluate_all`` /
            ``filter_candidates``, which batch-fetch for all symbols.
        spy_bars : pd.DataFrame, optional
            Pre-fetched SPY bars for the relative-strength check.
        """
        checks = {}
        reject_reasons = []

        if bars is None:
            bars = self._fetch_bars_single(symbol)
        if spy_bars is None:
            spy_bars = self._fetch_bars_single("SPY")
        has_bars = bars is not None and not bars.empty

        # 1. Already held? (pending entries count — don't re-screen a
        # symbol that already has a live entry order)
        if self._trade_db:
            positions = self._trade_db.get_open_positions(include_pending=True)
            held_symbols = {p["symbol"] for p in positions}
            already_held = symbol in held_symbols
            checks["already_held"] = not already_held
            if already_held:
                reject_reasons.append("already holding position")
        else:
            checks["already_held"] = True  # no DB = skip check

        # 2. Dollar volume (20-day average of close * volume)
        try:
            dollar_vol = 0.0
            if has_bars:
                dollar_vol = float((bars["close"] * bars["volume"]).tail(20).mean())
            checks["dollar_volume"] = dollar_vol >= self._params["min_dollar_volume"]
            checks["dollar_volume_value"] = dollar_vol
            if not checks["dollar_volume"]:
                reject_reasons.append(
                    f"dollar volume ${dollar_vol:,.0f} < ${self._params['min_dollar_volume']:,.0f}"
                )
        except Exception as exc:
            logger.warning("Dollar volume check failed for %s: %s", symbol, exc)
            checks["dollar_volume"] = False
            reject_reasons.append(f"dollar volume check error: {exc}")

        # 3. ADR% (14-day mean of (high - low) / close)
        try:
            adr_pct = 0.0
            if has_bars:
                daily_range_pct = (bars["high"] - bars["low"]) / bars["close"]
                adr_pct = float(daily_range_pct.tail(14).mean())
            checks["adr_pct"] = adr_pct >= self._params["min_adr_pct"]
            checks["adr_pct_value"] = round(adr_pct, 4)
            if not checks["adr_pct"]:
                reject_reasons.append(
                    f"ADR {adr_pct:.2%} < {self._params['min_adr_pct']:.0%}"
                )
        except Exception as exc:
            logger.warning("ADR check failed for %s: %s", symbol, exc)
            checks["adr_pct"] = False
            reject_reasons.append(f"ADR check error: {exc}")

        # 4. Price range
        try:
            if has_bars:
                latest_close = float(bars["close"].iloc[-1])
                in_range = self._params["min_price"] <= latest_close <= self._params["max_price"]
                checks["price_range"] = in_range
                checks["price"] = latest_close
                if not in_range:
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

        # 5. Relative strength vs SPY (20-day return difference)
        try:
            rs = self._relative_strength(bars, spy_bars, period=20)
            min_rs = self._params.get("min_rs_outperformance", 0.05)
            checks["relative_strength"] = rs >= min_rs  # must outperform by >= 5%
            checks["relative_strength_value"] = round(rs, 4)
            if not checks["relative_strength"]:
                reject_reasons.append(
                    f"RS {rs:.2%} < required {min_rs:.0%} above SPY"
                )
        except Exception as exc:
            logger.warning("RS check failed for %s: %s", symbol, exc)
            checks["relative_strength"] = False
            reject_reasons.append(f"RS check error: {exc}")

        # 6. Volume contraction (consolidation signal)
        try:
            if has_bars:
                vol = bars["volume"]
                recent_5d = vol.tail(5).mean()
                avg_20d = vol.tail(20).mean()
                contracting = recent_5d < avg_20d
                checks["volume_contraction"] = contracting
                checks["vol_ratio"] = round(recent_5d / avg_20d, 3) if avg_20d > 0 else 0
                # vol_ratio stored for quality scoring (lower = better)
            else:
                checks["volume_contraction"] = False
        except Exception as exc:
            logger.warning("Volume contraction check failed for %s: %s", symbol, exc)
            checks["volume_contraction"] = False

        # 7. Prior uptrend check (doc: "30%+ move in recent weeks/months")
        try:
            if has_bars:
                if len(bars) >= 40:
                    close = bars["close"]
                    # Find the max return from any point in the last 60 trading
                    # days to the current close — this captures the "pole" move.
                    current_close = float(close.iloc[-1])
                    lookback = close.tail(60)
                    min_price_in_window = float(lookback.min())
                    uptrend_pct = (current_close - min_price_in_window) / min_price_in_window
                    min_uptrend = self._params.get("min_prior_uptrend_pct", 0.30)
                    checks["prior_uptrend"] = uptrend_pct >= min_uptrend
                    checks["prior_uptrend_value"] = round(uptrend_pct, 4)
                    if not checks["prior_uptrend"]:
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
            if has_bars:
                close = bars["close"]
                s10 = float(close.rolling(window=10).mean().iloc[-1])
                s20 = float(close.rolling(window=20).mean().iloc[-1])
                s50 = float(close.rolling(window=50).mean().iloc[-1])
                stacked = s10 > s20 > s50
                checks["ma_stacking"] = stacked
                checks["ma_values"] = {"sma_10": round(s10, 2), "sma_20": round(s20, 2), "sma_50": round(s50, 2)}
                if not stacked:
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
        #    Provides pivot_high (entry trigger) and pivot_low (stop level).
        try:
            pivot = None
            if has_bars:
                pivot = self.data_client.compute_consolidation_pivot(
                    symbol, bars=bars, min_tight_days=2,
                )
            if pivot:
                checks["tight_consolidation"] = True
                checks["pivot_high"] = pivot["pivot_high"]
                checks["pivot_low"] = pivot["pivot_low"]
                checks["tight_days"] = pivot["tight_days"]
                # pivot data stored for quality scoring
            else:
                checks["tight_consolidation"] = False
                reject_reasons.append(
                    "no tight consolidation (< 2 days with range <= 2/3 ADR)"
                )
        except Exception as exc:
            logger.warning("Tight consolidation check failed for %s: %s", symbol, exc)
            checks["tight_consolidation"] = False
            reject_reasons.append(f"consolidation check error: {exc}")

        # 10. Whole-share sizing (account-size aware).
        #     Predict EXACTLY what the executor will size via the shared
        #     calculate_shares, using the pivot high as the entry trigger and
        #     the pivot low as the stop.  Unlike a price cap this keeps an
        #     expensive stock whose tight stop still admits a whole share, and
        #     drops a cheaper one whose wide stop does not.  Skipped when
        #     equity is unknown or dynamic sizing is off (gate passes).
        if (
            self._dynamic_sizing
            and self._portfolio_value
            and checks.get("tight_consolidation")
        ):
            ph = checks.get("pivot_high")
            pl = checks.get("pivot_low")
            sized = calculate_shares(
                ph, pl, self._portfolio_value,
                self._risk_pct,
                self._sizing["max_position_pct"],
                self._sizing["max_risk_pct"],
            )
            checks["share_sizing"] = sized >= 1
            checks["sized_shares"] = sized
            if sized < 1:
                reject_reasons.append(
                    f"sizes to {sized} shares at ${self._portfolio_value:,.0f} "
                    f"equity (stop ${ph - pl:.2f}/share vs "
                    f"${self._portfolio_value * self._risk_pct:.2f} "
                    f"risk budget)"
                )
        else:
            checks["share_sizing"] = True  # gate disabled / equity unknown

        # Determine pass/fail
        # Must pass: not already held, dollar volume, ADR, price range,
        # relative strength, prior uptrend, MA stacking, tight consolidation,
        # and whole-share sizing at the current account size.
        required_checks = [
            checks.get("already_held", False),
            checks.get("dollar_volume", False),
            checks.get("adr_pct", False),
            checks.get("price_range", False),
            checks.get("relative_strength", False),
            checks.get("prior_uptrend", False),
            checks.get("ma_stacking", False),
            checks.get("tight_consolidation", False),
            checks.get("share_sizing", True),
        ]
        passed = all(required_checks)

        # Compute continuous quality score (0-100) for ranking.
        # Only meaningful for passing candidates, but we compute it
        # unconditionally so the data is available for logging/debugging.
        quality_score = self.compute_quality_score(checks)

        return FilterResult(
            symbol=symbol,
            passed=passed,
            score=quality_score,
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


