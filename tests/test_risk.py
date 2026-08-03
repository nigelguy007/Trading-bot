"""The risk gate. These rules are what stand between the AI and the account."""
from __future__ import annotations

import pytest

from src.config import Settings
from src.models import Account, Position, Signal
from src.portfolio import PortfolioState
from src.risk import RiskManager


class StubPortfolio:
    """Only `group_for` is used by RiskManager."""

    GROUPS = {"AAPL": "big-tech", "MSFT": "big-tech", "NVDA": "semis", "XOM": "energy"}

    def group_for(self, symbol: str) -> str:
        return self.GROUPS.get(symbol.upper(), f"ungrouped:{symbol.upper()}")


@pytest.fixture
def settings() -> Settings:
    return Settings(
        anthropic_api_key="test",
        risk_per_trade_pct=1.0,
        daily_loss_limit_pct=3.0,
        max_drawdown_pct=15.0,
        max_open_positions=5,
        max_correlated_positions=2,
        max_portfolio_risk_pct=6.0,
        min_risk_reward=1.5,
        min_confidence=0.6,
        max_position_pct=50.0,
        _env_file=None,
    )


@pytest.fixture
def risk(settings) -> RiskManager:
    return RiskManager(settings, StubPortfolio())


def make_state(
    equity: float = 10_000.0,
    positions: list[Position] | None = None,
    open_risk: float = 0.0,
    drawdown_pct: float = 0.0,
    day_pnl_pct: float = 0.0,
) -> PortfolioState:
    return PortfolioState(
        account=Account(equity=equity, cash=equity, buying_power=equity, last_equity=equity),
        positions=positions or [],
        open_risk=open_risk,
        peak_equity=equity,
        drawdown_pct=drawdown_pct,
        day_pnl_pct=day_pnl_pct,
    )


def good_signal(**kw) -> Signal:
    defaults = dict(
        symbol="AAPL", direction="long", entry=100.0, stop=98.0, target=105.0,
        confidence=0.8, reasoning="test", invalidation=["loses 98"],
    )
    defaults.update(kw)
    return Signal(**defaults)


def position(symbol: str, qty: float = 10) -> Position:
    return Position(symbol=symbol, qty=qty, avg_entry=100.0, market_price=100.0, unrealized_pnl=0.0)


# --- circuit breakers -------------------------------------------------- #
def test_max_drawdown_halts_everything(risk):
    decision = risk.check_circuit_breakers(make_state(drawdown_pct=16.0))
    assert not decision.approved and decision.halt
    assert "MAX DRAWDOWN" in decision.reason


def test_daily_loss_limit_halts_everything(risk):
    decision = risk.check_circuit_breakers(make_state(day_pnl_pct=-3.5))
    assert not decision.approved and decision.halt
    assert "DAILY LOSS LIMIT" in decision.reason


def test_approaching_the_daily_limit_warns_but_allows(risk):
    decision = risk.check_circuit_breakers(make_state(day_pnl_pct=-2.5))
    assert decision.approved
    assert any("Approaching" in w for w in decision.warnings)


def test_quiet_day_passes_cleanly(risk):
    decision = risk.check_circuit_breakers(make_state(day_pnl_pct=0.4, drawdown_pct=1.0))
    assert decision.approved and not decision.warnings


# --- per-signal rules --------------------------------------------------- #
def test_a_clean_setup_is_approved(risk):
    decision = risk.evaluate(good_signal(), make_state())
    assert decision.approved, decision.reason
    assert decision.sizing.shares == 50


def test_no_stop_is_rejected(risk):
    decision = risk.evaluate(good_signal(stop=0.0), make_state())
    assert not decision.approved
    assert "No stop loss" in decision.reason


def test_stop_on_the_wrong_side_is_rejected(risk):
    # A long with its stop above entry is a broken plan, not a typo to fix.
    decision = risk.evaluate(good_signal(stop=102.0), make_state())
    assert not decision.approved
    assert "Incoherent" in decision.reason


def test_poor_risk_reward_is_rejected(risk):
    decision = risk.evaluate(good_signal(target=102.0), make_state())  # 2:1 risk -> R:R 1.0
    assert not decision.approved
    assert "R:R" in decision.reason


def test_low_confidence_is_rejected(risk):
    decision = risk.evaluate(good_signal(confidence=0.4), make_state())
    assert not decision.approved
    assert "Confidence" in decision.reason


def test_will_not_add_to_an_existing_position(risk):
    decision = risk.evaluate(good_signal(), make_state(positions=[position("AAPL")]))
    assert not decision.approved
    assert "Already holding" in decision.reason


def test_position_count_cap(risk):
    held = [position(s) for s in ("T", "F", "GE", "KO", "PG")]
    decision = risk.evaluate(good_signal(), make_state(positions=held))
    assert not decision.approved
    assert "position limit" in decision.reason


def test_correlated_positions_are_capped(risk):
    # Two big-tech names held; a third is the "five tech longs" failure mode.
    decision = risk.evaluate(
        good_signal(symbol="AAPL"),
        make_state(positions=[position("MSFT"), position("GOOGL")]),
    )
    # GOOGL isn't in the stub's map, so only MSFT counts as correlated here.
    assert decision.approved

    risk.portfolio.GROUPS["GOOGL"] = "big-tech"
    decision = risk.evaluate(
        good_signal(symbol="AAPL"),
        make_state(positions=[position("MSFT"), position("GOOGL")]),
    )
    assert not decision.approved
    assert "correlated" in decision.reason


def test_uncorrelated_positions_are_fine(risk):
    decision = risk.evaluate(
        good_signal(symbol="AAPL"),
        make_state(positions=[position("XOM"), position("NVDA")]),
    )
    assert decision.approved, decision.reason


def test_total_portfolio_risk_cap(risk):
    # 5.5% already at risk + 1% more would breach the 6% cap.
    decision = risk.evaluate(good_signal(), make_state(open_risk=550.0))
    assert not decision.approved
    assert "Total open risk" in decision.reason


def test_unsizeable_trade_is_rejected(risk):
    decision = risk.evaluate(good_signal(), make_state(equity=50.0))
    assert not decision.approved
    assert "rounds to zero" in decision.reason


def test_missing_invalidation_warns_without_blocking(risk):
    decision = risk.evaluate(good_signal(invalidation=[]), make_state())
    assert decision.approved
    assert any("invalidation" in w for w in decision.warnings)


def test_approved_size_never_exceeds_the_risk_budget(risk):
    decision = risk.evaluate(good_signal(), make_state())
    assert decision.sizing.account_risk_pct <= 1.0 + 1e-9
