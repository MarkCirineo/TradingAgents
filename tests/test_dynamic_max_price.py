"""Account-size-aware sizing: the coarse screen cap and the exact share model.

Two layers keep a small account tradeable without silently dropping entries:

1. ``get_effective_max_price`` -- a COARSE universe-scan ceiling at the
   position-size limit (10% of equity).  Above it, no stock fits even one
   whole share, so it's never tradeable regardless of stop.

2. ``calculate_shares`` -- the EXACT whole-share model shared by the executor
   (live sizing) and the pre-filter (predicting tradeability).  Because both
   call it, the pre-filter's keep/drop decision matches what the executor
   would really do -- no 7%-stop proxy, no over-filtering of tight-stop names.
"""

import copy

import pytest

from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.execution.executor import Executor
from tradingagents.strategies.swing_playbook import (
    calculate_shares,
    get_effective_max_price,
)

# Default sizing knobs (guardrails): 0.35% target risk, 10% position cap,
# 0.5% hard risk cap.
RISK, POS, MAXRISK = 0.0035, 0.10, 0.005


def _cfg(**overrides):
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    cfg["swing_strategy"].update(overrides)
    return cfg


# ---------------------------------------------------------------------------
# Coarse ceiling: get_effective_max_price
# ---------------------------------------------------------------------------

class TestEffectiveMaxPrice:
    @pytest.mark.parametrize("equity, expected", [
        (3_000, 300.0),    # 10% of 3k = position ceiling
        (4_000, 400.0),
        (5_000, 500.0),    # 10% of 5k hits the static ceiling exactly
        (6_000, 500.0),    # above 5k stays clamped at the static ceiling
        (25_000, 500.0),
    ])
    def test_scales_to_position_ceiling(self, equity, expected):
        assert get_effective_max_price(_cfg(), equity) == pytest.approx(expected)

    def test_never_exceeds_static_ceiling(self):
        assert get_effective_max_price(_cfg(), 1_000_000) == 500.0

    def test_floored_at_min_price(self):
        # 10% of $30 = $3, below min_price -> floored so the band isn't empty.
        assert get_effective_max_price(_cfg(min_price=5.0), 30) == 5.0

    def test_disabled_returns_static(self):
        assert get_effective_max_price(_cfg(dynamic_max_price=False), 3_000) == 500.0

    def test_missing_equity_returns_static(self):
        assert get_effective_max_price(_cfg(), None) == 500.0
        assert get_effective_max_price(_cfg(), 0) == 500.0

    def test_tracks_configured_position_cap(self):
        # The ceiling is the position-size guardrail, not a separate knob.
        cfg = copy.deepcopy(DEFAULT_CONFIG)
        cfg["guardrails"]["max_position_pct"] = 0.20
        assert get_effective_max_price(cfg, 2_000) == 400.0  # 20% of 2k

    def test_default_config_static_without_equity(self):
        assert get_effective_max_price(DEFAULT_CONFIG) == 500.0


# ---------------------------------------------------------------------------
# Exact model: calculate_shares
# ---------------------------------------------------------------------------

class TestCalculateShares:
    @pytest.mark.parametrize("entry, stop, equity, expected", [
        (100, 93, 4_000, 2),      # 7% stop: floor(14/7) = 2, no cap binds
        (250, 242.5, 4_000, 1),   # $250 @ 3% stop -> 1 share: KEPT (a price
                                  # cap at 5% of equity would have dropped it)
        (300, 279, 4_000, 0),     # 7% stop on a $300 name -> floor(14/21) = 0
        (450, 445, 4_000, 0),     # tight stop but $450 > 10% cap -> 0 shares
        (20, 19.9, 4_000, 20),    # ultra-tight stop -> position cap binds at 20
        (100, 100, 4_000, 0),     # zero stop distance
        (100, 105, 4_000, 0),     # inverted stop
        (100, 93, 0, 0),          # no equity
    ])
    def test_known_cases(self, entry, stop, equity, expected):
        assert calculate_shares(entry, stop, equity, RISK, POS, MAXRISK) == expected

    def test_risk_cap_branch_reduces_shares(self):
        # Force the hard-risk cap to bind (target > max_risk): entry 100, stop
        # 85 (=$15/share), $10k. risk-based 13 -> position cap 10 -> risk cap 6.
        assert calculate_shares(100, 85, 10_000, 0.02, 0.10, 0.01) == 6

    def test_the_original_over_filter_example(self):
        # The case that motivated option 2: $250 stock, 3% stop, $4k account.
        # Above the old 5%-of-equity ($200) price cap, yet a valid 1-share trade.
        assert calculate_shares(250, 242.5, 4_000, RISK, POS, MAXRISK) == 1

    def test_calm_regime_risk_flips_a_drop_to_a_keep(self):
        # entry 100, stop 85 ($15/share), $4k. At base 0.35% risk it floors to
        # 0; the calm-VIX regime's 0.40% risk floors to 1.  The pre-filter must
        # size with the regime risk or it would over-filter this in calm markets.
        assert calculate_shares(100, 85, 4_000, 0.0035, POS, MAXRISK) == 0
        assert calculate_shares(100, 85, 4_000, 0.0040, POS, MAXRISK) == 1


# ---------------------------------------------------------------------------
# Parity: the executor and the shared model must agree exactly
# ---------------------------------------------------------------------------

class TestExecutorParity:
    """calculate_position_size must return exactly what calculate_shares says,
    so the pre-filter's prediction equals live sizing."""

    @pytest.fixture
    def executor(self):
        return Executor(alpaca_client=None, trade_db=None, config=DEFAULT_CONFIG)

    @pytest.mark.parametrize("entry, stop, equity", [
        (100, 93, 4_000),
        (250, 242.5, 4_000),
        (60, 57, 5_000),
        (18.5, 17.9, 3_000),
        (300, 279, 4_000),
        (450, 445, 4_000),
        (75, 70, 100_000),
    ])
    def test_shares_match(self, executor, entry, stop, equity):
        expected = calculate_shares(entry, stop, equity, RISK, POS, MAXRISK)
        got = executor.calculate_position_size(entry, stop, equity)
        assert got["shares"] == expected

    def test_zero_share_case_is_a_clean_error(self, executor):
        # A count floored to 0 (by any cap) returns an error, not a 0-qty order.
        got = executor.calculate_position_size(300, 279, 4_000)
        assert got["shares"] == 0
        assert "error" in got

    def test_inverted_stop_errors(self, executor):
        got = executor.calculate_position_size(100, 105, 4_000)
        assert got["shares"] == 0
        assert got["error"] == "stop_price must be below entry_price"

    def test_regime_adjusted_risk_matches(self):
        # With a calm regime (risk bumped to 0.40%), the executor and the
        # shared model must still agree -- this is what the pre-filter mirrors.
        ex = Executor(alpaca_client=None, trade_db=None,
                      config=DEFAULT_CONFIG, regime={"risk_pct": 0.0040})
        got = ex.calculate_position_size(100, 85, 4_000)["shares"]
        assert got == calculate_shares(100, 85, 4_000, 0.0040, POS, MAXRISK)
        assert got == 1  # the flip case: base risk would have been 0
