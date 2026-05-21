"""Unit tests for the risk agent's hard check logic."""
import pytest
from unittest.mock import MagicMock, patch
from agents.risk_agent import RiskAgent, PortfolioState
from agents.edge_detector import EdgeResult
from config.settings import settings


def _make_edge(has_edge=True, confidence=0.75, direction="yes", edge=0.12) -> EdgeResult:
    e = MagicMock(spec=EdgeResult)
    e.has_edge = has_edge
    e.confidence = confidence
    e.direction = direction
    e.edge = edge
    e.market_id = "test-market-001"
    e.model_yes_prob = 0.62
    e.kelly_fraction = 0.08
    e.expected_value = 0.24
    return e


def _make_portfolio(**kwargs) -> PortfolioState:
    defaults = dict(
        bankroll=1000.0,
        daily_pnl=0.0,
        open_positions_value=0.0,
        open_position_count=0,
        daily_trades=0,
    )
    defaults.update(kwargs)
    return PortfolioState(**defaults)


class TestRiskAgentHardChecks:
    def setup_method(self):
        self.agent = RiskAgent()

    def test_no_edge_blocked(self):
        edge = _make_edge(has_edge=False)
        portfolio = _make_portfolio()
        result = self.agent._hard_checks(edge, portfolio)
        assert result is not None
        assert "edge" in result.lower()

    def test_low_confidence_blocked(self):
        edge = _make_edge(confidence=0.40)
        portfolio = _make_portfolio()
        result = self.agent._hard_checks(edge, portfolio)
        assert result is not None
        assert "confidence" in result.lower()

    def test_daily_loss_limit_blocked(self):
        edge = _make_edge()
        portfolio = _make_portfolio(daily_pnl=-settings.max_daily_loss_usd)
        result = self.agent._hard_checks(edge, portfolio)
        assert result is not None
        assert "daily loss" in result.lower()

    def test_max_exposure_blocked(self):
        edge = _make_edge()
        portfolio = _make_portfolio(
            open_positions_value=settings.max_portfolio_exposure_usd
        )
        result = self.agent._hard_checks(edge, portfolio)
        assert result is not None

    def test_healthy_portfolio_passes(self):
        edge = _make_edge()
        portfolio = _make_portfolio()
        result = self.agent._hard_checks(edge, portfolio)
        assert result is None
