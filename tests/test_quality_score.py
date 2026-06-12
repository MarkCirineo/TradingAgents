"""Tests for the candidate quality scoring system in pre_filter.py.

Validates:
  - compute_quality_score produces correct scores for known inputs
  - Candidates are correctly ranked (higher quality = higher score)
  - Edge cases (minimum values, maximum values, missing data)
  - Proximity scoring (below/at/above pivot)
"""

import pytest

from tradingagents.screening.pre_filter import PreFilter


# ---------------------------------------------------------------------------
# Helpers: build synthetic check dicts
# ---------------------------------------------------------------------------

def _make_checks(
    rs_value=0.05,
    vol_ratio=1.0,
    tight_days=2,
    uptrend_value=0.30,
    adr_pct_value=0.04,
    price=50.0,
    pivot_high=52.0,
    pivot_low=49.0,
    dollar_volume_value=50_000_000,
):
    """Build a checks dict with controllable values for quality scoring."""
    return {
        "relative_strength_value": rs_value,
        "vol_ratio": vol_ratio,
        "tight_days": tight_days,
        "prior_uptrend_value": uptrend_value,
        "adr_pct_value": adr_pct_value,
        "price": price,
        "pivot_high": pivot_high,
        "pivot_low": pivot_low,
        "dollar_volume_value": dollar_volume_value,
    }


# ---------------------------------------------------------------------------
# Test: Score ranges and known outputs
# ---------------------------------------------------------------------------

class TestComputeQualityScore:
    """Test compute_quality_score with deterministic inputs."""

    def test_all_minimum_values_score_near_zero(self):
        """Bare-minimum passing values should produce score ~10 (only proximity)."""
        checks = _make_checks(
            rs_value=0.05,       # exactly at threshold
            vol_ratio=1.0,       # no contraction at all
            tight_days=2,        # minimum
            uptrend_value=0.30,  # exactly at threshold
            adr_pct_value=0.04,  # exactly at threshold
            price=52.0,          # at pivot (still good proximity)
            pivot_high=52.0,
            pivot_low=49.0,
            dollar_volume_value=50_000_000,
        )
        score = PreFilter.compute_quality_score(checks)
        # RS=0, tightness=0, uptrend=0, ADR=0, proximity=1.0 (at pivot), dvol=0
        # Only proximity contributes: 0.10 * 100 = 10.0
        assert 5.0 <= score <= 15.0, f"Expected ~10, got {score}"

    def test_all_maximum_values_score_100(self):
        """Best possible values across all dimensions should score 100."""
        checks = _make_checks(
            rs_value=0.35,       # well above 30% cap
            vol_ratio=0.30,      # very tight volume contraction
            tight_days=8,        # many tight days
            uptrend_value=1.20,  # 120% uptrend
            adr_pct_value=0.15,  # 15% ADR
            price=48.0,          # below pivot (best proximity)
            pivot_high=52.0,
            pivot_low=49.0,
            dollar_volume_value=600_000_000,  # high liquidity
        )
        score = PreFilter.compute_quality_score(checks)
        assert score == 100.0, f"Expected 100, got {score}"

    def test_midrange_values(self):
        """Midrange values should produce a moderate score."""
        checks = _make_checks(
            rs_value=0.175,      # midpoint between 0.05 and 0.30
            vol_ratio=0.70,      # some contraction
            tight_days=4,        # moderate
            uptrend_value=0.65,  # midpoint between 0.30 and 1.00
            adr_pct_value=0.08,  # midpoint between 0.04 and 0.12
            price=50.0,          # below pivot
            pivot_high=52.0,
            pivot_low=49.0,
            dollar_volume_value=275_000_000,
        )
        score = PreFilter.compute_quality_score(checks)
        assert 40.0 <= score <= 60.0, f"Expected ~50, got {score}"

    def test_score_is_float_with_one_decimal(self):
        """Score should be rounded to one decimal place."""
        checks = _make_checks(rs_value=0.123)
        score = PreFilter.compute_quality_score(checks)
        assert score == round(score, 1)


# ---------------------------------------------------------------------------
# Test: Relative ordering (ranking correctness)
# ---------------------------------------------------------------------------

class TestQualityRanking:
    """Verify that better setups always rank higher."""

    def test_higher_rs_ranks_higher(self):
        """Stock with stronger relative strength should score higher."""
        weak = _make_checks(rs_value=0.06)
        strong = _make_checks(rs_value=0.25)
        assert PreFilter.compute_quality_score(strong) > PreFilter.compute_quality_score(weak)

    def test_tighter_consolidation_ranks_higher(self):
        """Tighter volume contraction + more days should score higher."""
        loose = _make_checks(vol_ratio=0.95, tight_days=2)
        tight = _make_checks(vol_ratio=0.50, tight_days=5)
        assert PreFilter.compute_quality_score(tight) > PreFilter.compute_quality_score(loose)

    def test_bigger_uptrend_ranks_higher(self):
        """Stronger prior uptrend should score higher."""
        weak_trend = _make_checks(uptrend_value=0.32)
        strong_trend = _make_checks(uptrend_value=0.85)
        assert PreFilter.compute_quality_score(strong_trend) > PreFilter.compute_quality_score(weak_trend)

    def test_higher_adr_ranks_higher(self):
        """Higher ADR% means more opportunity per trade."""
        low_adr = _make_checks(adr_pct_value=0.045)
        high_adr = _make_checks(adr_pct_value=0.10)
        assert PreFilter.compute_quality_score(high_adr) > PreFilter.compute_quality_score(low_adr)

    def test_closer_to_pivot_ranks_higher(self):
        """Stock closer to pivot should rank higher than extended one."""
        at_pivot = _make_checks(price=52.0, pivot_high=52.0, pivot_low=49.0)
        extended = _make_checks(price=54.5, pivot_high=52.0, pivot_low=49.0)
        assert PreFilter.compute_quality_score(at_pivot) > PreFilter.compute_quality_score(extended)

    def test_rs_dominates_ranking(self):
        """RS has the highest weight (30%), so a big RS difference should
        outweigh small advantages in other dimensions."""
        # Great RS, average everything else
        great_rs = _make_checks(rs_value=0.28, vol_ratio=0.80, tight_days=3)
        # Average RS, great tightness
        great_tight = _make_checks(rs_value=0.08, vol_ratio=0.45, tight_days=6)
        assert PreFilter.compute_quality_score(great_rs) > PreFilter.compute_quality_score(great_tight)


# ---------------------------------------------------------------------------
# Test: Proximity scoring edge cases
# ---------------------------------------------------------------------------

class TestProximityScoring:
    """Test pivot proximity scoring logic."""

    def test_below_pivot_is_best(self):
        """Price below pivot should give max proximity score."""
        checks = _make_checks(price=48.0, pivot_high=52.0, pivot_low=49.0)
        score_below = PreFilter.compute_quality_score(checks)
        checks_at = _make_checks(price=52.0, pivot_high=52.0, pivot_low=49.0)
        score_at = PreFilter.compute_quality_score(checks_at)
        # Both should get 1.0 proximity, so scores should be equal
        assert score_below == score_at

    def test_above_pivot_decays_linearly(self):
        """Proximity score should decrease as price moves above pivot."""
        pivot_high, pivot_low = 52.0, 49.0

        score_at = PreFilter.compute_quality_score(
            _make_checks(price=52.0, pivot_high=pivot_high, pivot_low=pivot_low)
        )
        score_half_r = PreFilter.compute_quality_score(
            _make_checks(price=53.5, pivot_high=pivot_high, pivot_low=pivot_low)
        )
        score_full_r = PreFilter.compute_quality_score(
            _make_checks(price=55.0, pivot_high=pivot_high, pivot_low=pivot_low)
        )

        assert score_at > score_half_r > score_full_r

    def test_missing_pivot_data_gives_neutral_score(self):
        """Missing pivot data should give a neutral (0.5) proximity score."""
        checks = _make_checks(price=50.0, pivot_high=0, pivot_low=0)
        score = PreFilter.compute_quality_score(checks)
        # Should not crash and should produce a reasonable score
        assert 0 <= score <= 100


# ---------------------------------------------------------------------------
# Test: Edge cases
# ---------------------------------------------------------------------------

class TestEdgeCases:
    """Test boundary conditions and missing data."""

    def test_empty_checks_dict(self):
        """Empty checks dict should produce a small score, not crash."""
        score = PreFilter.compute_quality_score({})
        assert 0 <= score <= 100

    def test_extreme_values_clamp_correctly(self):
        """Values far beyond the normalization range should clamp to 1.0."""
        checks = _make_checks(
            rs_value=2.0,  # 200% outperformance (insane, should clamp)
            uptrend_value=5.0,  # 500% uptrend
            adr_pct_value=0.50,  # 50% ADR
        )
        score = PreFilter.compute_quality_score(checks)
        assert score <= 100.0

    def test_negative_values_clamp_to_zero(self):
        """Negative metric values should clamp sub-scores to 0, not go negative."""
        checks = _make_checks(
            rs_value=-0.10,  # negative RS (stock underperforming)
            uptrend_value=-0.20,  # downtrend
        )
        score = PreFilter.compute_quality_score(checks)
        assert score >= 0.0
