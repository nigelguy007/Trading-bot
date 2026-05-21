"""Unit tests for edge detection math — no API calls required."""
import pytest
from agents.edge_detector import _kelly_fraction, _expected_value


class TestKellyFraction:
    def test_positive_edge(self):
        # Model says 60%, market says 50%
        kf = _kelly_fraction(prob=0.60, market_prob=0.50)
        assert kf > 0.0
        assert kf <= 0.25  # capped at max_kelly

    def test_no_edge(self):
        # Model agrees with market
        kf = _kelly_fraction(prob=0.50, market_prob=0.50)
        assert kf == 0.0

    def test_negative_edge(self):
        # Model says 40%, market says 50% — no positive edge on YES
        kf = _kelly_fraction(prob=0.40, market_prob=0.50)
        assert kf == 0.0

    def test_extreme_edge_capped(self):
        # Huge edge should still be capped at max_kelly
        kf = _kelly_fraction(prob=0.99, market_prob=0.10)
        assert kf == 0.25

    def test_invalid_market_prob_zero(self):
        kf = _kelly_fraction(prob=0.80, market_prob=0.0)
        assert kf == 0.0


class TestExpectedValue:
    def test_positive_ev(self):
        # Model 60%, market 50% → should be positive EV
        ev = _expected_value(prob=0.60, market_prob=0.50)
        assert ev > 0.0

    def test_zero_edge(self):
        ev = _expected_value(prob=0.50, market_prob=0.50)
        assert abs(ev) < 1e-9

    def test_negative_ev(self):
        ev = _expected_value(prob=0.40, market_prob=0.50)
        assert ev < 0.0
