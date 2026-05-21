"""Unit tests for the Bayesian probability model."""
import pytest
from unittest.mock import MagicMock
from models.probability_model import ProbabilityModel
from agents.sentiment_agent import SentimentSignal


def _make_sentiment(composite: float, crowd: float, confidence: float) -> SentimentSignal:
    s = MagicMock(spec=SentimentSignal)
    s.composite_sentiment = composite
    s.crowd_estimate = crowd
    s.confidence = confidence
    return s


class TestProbabilityModel:
    def setup_method(self):
        self.model = ProbabilityModel()

    def test_bullish_sentiment_increases_prob(self):
        sentiment = _make_sentiment(composite=0.8, crowd=0.65, confidence=0.8)
        result = self.model.calibrate(market_price=0.50, sentiment=sentiment)
        assert result.posterior > 0.50

    def test_bearish_sentiment_decreases_prob(self):
        sentiment = _make_sentiment(composite=-0.8, crowd=0.35, confidence=0.8)
        result = self.model.calibrate(market_price=0.50, sentiment=sentiment)
        assert result.posterior < 0.50

    def test_low_confidence_returns_prior(self):
        sentiment = _make_sentiment(composite=0.9, crowd=0.9, confidence=0.1)
        result = self.model.calibrate(market_price=0.50, sentiment=sentiment)
        assert result.posterior == result.prior

    def test_posterior_bounded(self):
        sentiment = _make_sentiment(composite=1.0, crowd=1.0, confidence=1.0)
        result = self.model.calibrate(market_price=0.99, sentiment=sentiment)
        assert 0.0 < result.posterior < 1.0

    def test_blend_weights_sum_to_one(self):
        blended = self.model.blend(0.4, 0.6, 0.5, weights=(1, 1, 1))
        assert abs(blended - 0.5) < 1e-6
