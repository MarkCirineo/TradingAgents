"""Swing Trading Playbook -- strategy configuration and prompt generation.

This module codifies the momentum breakout methodology from the expert
document (Qullamaggie / peoplewish) into two outputs:

1. **Agent prompt overlay** -- the A+ entry criteria checklist, injected
   into the Trader and Portfolio Manager prompts when ``trading_mode``
   is ``"daemon"``.  Only entry criteria go to the LLM; exit rules
   and position sizing are executed as deterministic code in
   ``position_manager.py`` and ``executor.py``.

2. **Configuration accessors** -- typed helpers that return strategy
   parameters from ``DEFAULT_CONFIG["swing_strategy"]`` and
   ``DEFAULT_CONFIG["guardrails"]`` for use by the hard-coded
   position-management and sizing logic.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from tradingagents.default_config import DEFAULT_CONFIG

# ---------------------------------------------------------------------------
# Entry criteria prompt text (injected into Trader + PM in daemon mode)
# ---------------------------------------------------------------------------

_ENTRY_CRITERIA_PROMPT = """\
=== SWING TRADING ENTRY CRITERIA (A+ Setup Checklist) ===

You are evaluating this stock as a potential SWING TRADE entry. The holding
period is not fixed -- exits are driven by the 10-day SMA trailing stop.
Winners can run for days, weeks, or longer as long as they stay above the
trailing MA.

ALREADY VERIFIED BY THE PRE-FILTER (you do not need to check these):
- Market regime is favorable (SPY above rising 20 MA, 10 MA > 20 MA)
- Stock has sufficient liquidity (dollar volume > $50M)
- Stock has sufficient volatility (ADR > 4%)
- Stock is outperforming SPY by at least 5% over 20 days
- Stock has a prior uptrend of at least 30% over 60 days
- Stock's moving averages are stacked bullishly (10 > 20 > 50)

YOUR JOB -- Evaluate the following. ALL must be met for a Buy recommendation.
If ANY criterion fails, recommend Hold or Sell.

1. SETUP PATTERN -- Is this a breakout from tight consolidation?
   - Price consolidating near/above rising 10-day or 20-day MA
   - Consolidation is orderly (tight range, not erratic/volatile)
   - Volume drying up during consolidation (contraction = coiling)
   - The consolidation lasted at least several days

2. ENTRY TRIGGER -- Is the breakout confirmed?
   - Price breaking above consolidation resistance on INCREASING volume
   - Volume on breakout day should be notably above average
   - Ideally within the first 1-3 days of the breakout move

DO NOT recommend entry if:
- No clear consolidation pattern exists
- Volume is declining on the breakout attempt
- The stock has already extended far beyond its consolidation (chasing)
- The consolidation is wide and sloppy rather than tight

Source: Qullamaggie momentum breakout methodology + peoplewish execution rules.
=== END SWING TRADING CRITERIA ==="""


# ---------------------------------------------------------------------------
# Prompt accessor (for agent injection)
# ---------------------------------------------------------------------------

def get_entry_criteria_prompt() -> str:
    """Return the A+ entry criteria prompt text.

    This is injected into the Trader and Portfolio Manager system prompts
    when ``config["trading_mode"] == "daemon"``.
    """
    return _ENTRY_CRITERIA_PROMPT


def is_daemon_mode(config: Optional[Dict[str, Any]] = None) -> bool:
    """Return ``True`` if the system is running in autonomous daemon mode."""
    cfg = config or DEFAULT_CONFIG
    return cfg.get("trading_mode") == "daemon"


# ---------------------------------------------------------------------------
# Exit-rule configuration (consumed by position_manager.py as CODE)
# ---------------------------------------------------------------------------

def get_exit_rules(config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Return the exit-rule parameters for position_manager.py.

    These are NOT sent to the LLM.  They drive deterministic code logic.
    """
    cfg = config or DEFAULT_CONFIG
    ss = cfg.get("swing_strategy", {})
    return {
        "day1_red_close_exit": ss.get("day1_red_close_exit", True),
        "partial_profit_day": ss.get("partial_profit_day", 3),
        "partial_profit_pct": ss.get("partial_profit_pct", 0.50),
        "trailing_ma_period": ss.get("trailing_ma_period", 10),
        "trailing_ma_exit_on": ss.get("trailing_ma_exit_on", "close"),
        "max_extension_adr_multiple": ss.get("max_extension_adr_multiple", 7),
        "soft_backstop_days": ss.get("soft_backstop_days", 30),
    }


# ---------------------------------------------------------------------------
# Sizing configuration (consumed by executor.py as CODE)
# ---------------------------------------------------------------------------

def get_sizing_params(config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Return position-sizing parameters for executor.py.

    These are NOT sent to the LLM.  They drive deterministic math.
    """
    cfg = config or DEFAULT_CONFIG
    gr = cfg.get("guardrails", {})
    return {
        "target_risk_pct": gr.get("target_risk_per_trade_pct", 0.0035),
        "max_risk_pct": gr.get("max_risk_per_trade_pct", 0.005),
        "max_position_pct": gr.get("max_position_pct", 0.10),
        "max_exposure_pct": gr.get("max_exposure_pct", 0.60),
        "max_concurrent_positions": gr.get("max_concurrent_positions", 6),
    }


# ---------------------------------------------------------------------------
# Screening configuration (consumed by screener.py / pre_filter.py as CODE)
# ---------------------------------------------------------------------------

def get_screening_params(config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Return screening/filtering parameters.

    These are NOT sent to the LLM.
    """
    cfg = config or DEFAULT_CONFIG
    ss = cfg.get("swing_strategy", {})
    return {
        "min_prior_uptrend_pct": ss.get("min_prior_uptrend_pct", 0.30),
        "min_rs_percentile": ss.get("min_rs_percentile", 0.85),
        "min_rs_outperformance": ss.get("min_rs_outperformance", 0.05),
        "min_dollar_volume": ss.get("min_dollar_volume", 50_000_000),
        "min_adr_pct": ss.get("min_adr_pct", 0.04),
        "min_price": ss.get("min_price", 5.0),
        "max_price": ss.get("max_price", 500.0),
        "orh_window_minutes": ss.get("orh_window_minutes", 15),
    }


# ---------------------------------------------------------------------------
# VIX regime adjustments (modulates sizing, not a binary gate)
# ---------------------------------------------------------------------------

# The primary regime filter is SPY MA stacking (check_market_regime).
# VIX is a secondary modulator that adjusts position sizing and exposure
# rather than blocking trades entirely.  This aligns with the Qullamaggie
# methodology: become more selective and smaller in choppy conditions,
# rather than stopping completely.
#
# VIX > 30 is the exception: breakout setups fail almost universally in
# panic environments, so we pause NEW entries (existing positions are
# still managed via trailing stops / trims).

_VIX_REGIMES = {
    # (vix_floor, vix_ceiling): {adjustments}
    "calm":     {"vix_max": 15,  "risk_pct": 0.0040, "max_positions": 6, "max_exposure_pct": 0.60, "pause_entries": False, "label": "Calm"},
    "normal":   {"vix_max": 20,  "risk_pct": 0.0035, "max_positions": 6, "max_exposure_pct": 0.60, "pause_entries": False, "label": "Normal"},
    "elevated": {"vix_max": 30,  "risk_pct": 0.0025, "max_positions": 4, "max_exposure_pct": 0.40, "pause_entries": False, "label": "Elevated"},
    "panic":    {"vix_max": 999, "risk_pct": 0.0,    "max_positions": 0, "max_exposure_pct": 0.0,  "pause_entries": True,  "label": "Panic"},
}


def get_regime_adjustments(vix_level: float) -> Dict[str, Any]:
    """Return adjusted trading parameters based on current VIX level.

    These override the static defaults from ``get_sizing_params()`` when
    the executor calculates position size for a new entry.

    Parameters
    ----------
    vix_level : float
        Current CBOE VIX index level.

    Returns
    -------
    dict
        ``risk_pct`` -- adjusted risk per trade (0 = no new entries).
        ``max_positions`` -- adjusted max concurrent positions.
        ``max_exposure_pct`` -- adjusted max portfolio exposure.
        ``pause_entries`` -- if True, do not open any new positions.
        ``label`` -- human-readable regime name.
        ``vix_level`` -- the input VIX value (for logging).
    """
    if vix_level > 30:
        regime = _VIX_REGIMES["panic"]
    elif vix_level > 20:
        regime = _VIX_REGIMES["elevated"]
    elif vix_level < 15:
        regime = _VIX_REGIMES["calm"]
    else:
        regime = _VIX_REGIMES["normal"]

    return {**regime, "vix_level": vix_level}

