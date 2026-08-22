"""Tests for the portfolio-heat and sector-concentration guardrails.

Position count is a backstop; these two checks are the real concentration
controls.  Heat sums open risk -- (entry - stop) x qty -- so tight setups
earn more slots than sloppy ones.  The sector cap exists because momentum
leaders cluster into themes, so N positions can amount to a single bet.

Covers:
  - heat accumulates across open positions and blocks past the cap
  - breakeven / trimmed positions contribute ~0 heat
  - PENDING entries reserve heat and sector room
  - sector cap blocks a same-sector add, allows a different sector
  - sector lookup failure fails open rather than halting trading
  - regime tightens heat and sector caps as VIX rises
"""

from types import SimpleNamespace

import pytest

from tradingagents.dataflows import sector as sector_mod
from tradingagents.execution.guardrails import Guardrails
from tradingagents.execution.trade_db import TradeDB

PORTFOLIO = 100_000.0


@pytest.fixture
def db(tmp_path):
    return TradeDB(db_path=str(tmp_path / "heat.db"))


@pytest.fixture(autouse=True)
def _clean_sector_cache():
    sector_mod.clear_cache()
    yield
    sector_mod.clear_cache()


class _FakeClient:
    """Minimal Alpaca stand-in: portfolio value + live position values."""

    def __init__(self, positions=None):
        self._positions = positions or {}

    def get_portfolio_value(self):
        return PORTFOLIO

    def get_all_positions(self):
        return [
            SimpleNamespace(symbol=s, market_value=v)
            for s, v in self._positions.items()
        ]


def _open(db, symbol, entry, stop, qty, sector=None, status="OPEN"):
    db.open_position(
        symbol=symbol,
        entry_date="2026-08-20",
        entry_price=entry,
        entry_orl=stop,
        qty=qty,
        sector=sector,
        status=status,
    )


# ---------------------------------------------------------------------------
# Portfolio heat
# ---------------------------------------------------------------------------

class TestPortfolioHeat:
    def test_heat_sums_open_risk(self, db):
        # 3 positions x $100 entry, $95 stop, 60 shares = $300 risk each.
        for i in range(3):
            _open(db, f"SYM{i}", 100.0, 95.0, 60, sector="Energy")
        gr = Guardrails(alpaca_client=_FakeClient(), trade_db=db)
        assert gr._open_risk(db.get_open_positions()) == pytest.approx(900.0)

    def test_blocks_when_heat_cap_exceeded(self, db):
        # Cap is 3% of $100k = $3,000.  Nine positions at $300 = $2,700;
        # a tenth at $500 would reach $3,200.
        for i in range(9):
            _open(db, f"SYM{i}", 100.0, 95.0, 60, sector="Energy")
        gr = Guardrails(alpaca_client=_FakeClient(), trade_db=db)
        result = gr.validate_entry("NEW", proposed_value=5_000, proposed_risk=500.0)
        assert not result.approved
        assert "portfolio heat" in result.reason

    def test_allows_when_under_heat_cap(self, db):
        for i in range(5):
            _open(db, f"SYM{i}", 100.0, 95.0, 60, sector="Energy")
        gr = Guardrails(alpaca_client=_FakeClient(), trade_db=db)
        result = gr.validate_entry("NEW", proposed_value=5_000, proposed_risk=350.0)
        assert result.approved, result.reason
        assert result.checks["new_heat"] == pytest.approx(1_850.0)

    def test_breakeven_stop_contributes_no_heat(self, db):
        # Stop raised to entry -- no account risk left, so it must not
        # consume heat.  Risk is floored at zero, never negative.
        _open(db, "BE", 100.0, 100.0, 60, sector="Energy")
        _open(db, "ABOVE", 100.0, 110.0, 60, sector="Energy")
        gr = Guardrails(alpaca_client=_FakeClient(), trade_db=db)
        assert gr._open_risk(db.get_open_positions()) == pytest.approx(0.0)

    def test_pending_entries_reserve_heat(self, db):
        for i in range(9):
            _open(db, f"P{i}", 100.0, 95.0, 60, sector="Energy", status="PENDING")
        gr = Guardrails(alpaca_client=_FakeClient(), trade_db=db)
        result = gr.validate_entry("NEW", proposed_value=5_000, proposed_risk=500.0)
        assert not result.approved
        assert "portfolio heat" in result.reason

    def test_heat_check_skipped_without_risk(self, db):
        """Callers that omit proposed_risk keep the old behaviour."""
        for i in range(9):
            _open(db, f"SYM{i}", 100.0, 95.0, 60, sector="Energy")
        gr = Guardrails(alpaca_client=_FakeClient(), trade_db=db)
        result = gr.validate_entry("NEW", proposed_value=1_000)
        assert result.approved, result.reason
        assert "new_heat" not in result.checks

    def test_heat_binds_before_count(self, db):
        """The point of the redesign: heat stops us before the count does."""
        for i in range(8):
            _open(db, f"SYM{i}", 100.0, 95.0, 60, sector="Energy")
        gr = Guardrails(alpaca_client=_FakeClient(), trade_db=db)
        result = gr.validate_entry("NEW", proposed_value=5_000, proposed_risk=700.0)
        assert not result.approved
        # 8 of 10 slots used -- the count check passed, heat is what blocked.
        assert "portfolio heat" in result.reason
        assert result.checks["positions_count"] == 8


# ---------------------------------------------------------------------------
# Sector concentration
# ---------------------------------------------------------------------------

class TestSectorCap:
    def test_blocks_same_sector_over_cap(self, db, monkeypatch):
        # Cap 30% of $100k = $30,000.  Three held Tech names at $9k each.
        for i in range(3):
            _open(db, f"TECH{i}", 100.0, 95.0, 90, sector="Technology")
        client = _FakeClient({f"TECH{i}": 9_000.0 for i in range(3)})
        monkeypatch.setattr(sector_mod, "get_sector", lambda s: "Technology")

        gr = Guardrails(alpaca_client=client, trade_db=db)
        result = gr.validate_entry("NVDA", proposed_value=5_000, proposed_risk=100.0)
        assert not result.approved
        assert "Technology exposure" in result.reason
        assert result.checks["new_sector_exposure"] == pytest.approx(32_000.0)

    def test_allows_different_sector(self, db, monkeypatch):
        for i in range(3):
            _open(db, f"TECH{i}", 100.0, 95.0, 90, sector="Technology")
        client = _FakeClient({f"TECH{i}": 9_000.0 for i in range(3)})
        sectors = {"XOM": "Energy"}
        monkeypatch.setattr(
            sector_mod, "get_sector",
            lambda s: sectors.get(s, "Technology"),
        )

        gr = Guardrails(alpaca_client=client, trade_db=db)
        result = gr.validate_entry("XOM", proposed_value=5_000, proposed_risk=100.0)
        assert result.approved, result.reason
        assert result.checks["current_sector_exposure"] == pytest.approx(0.0)

    def test_pending_position_reserves_sector_room(self, db, monkeypatch):
        """A PENDING entry has no broker position, so its notional is used."""
        for i in range(3):
            _open(db, f"TECH{i}", 100.0, 95.0, 90,
                  sector="Technology", status="PENDING")
        monkeypatch.setattr(sector_mod, "get_sector", lambda s: "Technology")

        gr = Guardrails(alpaca_client=_FakeClient(), trade_db=db)
        result = gr.validate_entry("NVDA", proposed_value=5_000, proposed_risk=100.0)
        assert not result.approved
        assert "Technology exposure" in result.reason

    def test_fails_open_when_sector_unavailable(self, db, monkeypatch):
        """A Yahoo outage must not halt trading."""
        for i in range(3):
            _open(db, f"TECH{i}", 100.0, 95.0, 90, sector="Technology")
        client = _FakeClient({f"TECH{i}": 9_000.0 for i in range(3)})
        monkeypatch.setattr(sector_mod, "get_sector", lambda s: None)

        gr = Guardrails(alpaca_client=client, trade_db=db)
        result = gr.validate_entry("NVDA", proposed_value=5_000, proposed_risk=100.0)
        assert result.approved, result.reason
        assert result.checks["sector_check_skipped"] is True

    def test_stored_sector_avoids_network_lookup(self, db, monkeypatch):
        """Sectors on position rows prime the cache; only the new symbol is fetched."""
        for i in range(3):
            _open(db, f"TECH{i}", 100.0, 95.0, 90, sector="Technology")
        client = _FakeClient({f"TECH{i}": 9_000.0 for i in range(3)})

        fetched = []

        def _fake_info(symbol):
            fetched.append(symbol)
            return "Technology"

        monkeypatch.setattr(sector_mod, "_fetch_sector", _fake_info)
        gr = Guardrails(alpaca_client=client, trade_db=db)
        gr.validate_entry("NVDA", proposed_value=5_000, proposed_risk=100.0)
        assert fetched == ["NVDA"]


# ---------------------------------------------------------------------------
# Regime interaction
# ---------------------------------------------------------------------------

class TestRegimeScaling:
    def test_elevated_regime_tightens_caps(self, db):
        from tradingagents.strategies.swing_playbook import get_regime_adjustments

        regime = get_regime_adjustments(25.0)  # Elevated
        gr = Guardrails(alpaca_client=_FakeClient(), trade_db=db, regime=regime)
        assert gr._max_heat_pct == 0.015
        assert gr._max_sector_pct == 0.20
        assert gr._max_concurrent == 6

    def test_elevated_heat_cap_blocks_earlier(self, db):
        from tradingagents.strategies.swing_playbook import get_regime_adjustments

        # $1,500 cap at Elevated vs $3,000 at Normal.
        for i in range(4):
            _open(db, f"SYM{i}", 100.0, 95.0, 60, sector="Energy")
        regime = get_regime_adjustments(25.0)
        gr = Guardrails(alpaca_client=_FakeClient(), trade_db=db, regime=regime)
        result = gr.validate_entry("NEW", proposed_value=5_000, proposed_risk=350.0)
        assert not result.approved
        assert "portfolio heat" in result.reason

    def test_unexpected_sector_error_fails_open(self, db, monkeypatch):
        """An exception inside the sector check must not halt trading."""
        for i in range(3):
            _open(db, f"TECH{i}", 100.0, 95.0, 90, sector="Technology")
        client = _FakeClient({f"TECH{i}": 9_000.0 for i in range(3)})

        def _boom(_symbol):
            raise RuntimeError("yahoo exploded")

        monkeypatch.setattr(sector_mod, "get_sector", _boom)
        gr = Guardrails(alpaca_client=client, trade_db=db)
        result = gr.validate_entry("NVDA", proposed_value=5_000, proposed_risk=100.0)
        assert result.approved, result.reason
        assert result.checks["sector_check_skipped"] is True
