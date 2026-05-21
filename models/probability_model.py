"""
Probability Model
Combines market price, sentiment signal, and base-rate reasoning into
a calibrated probability estimate using a simple Bayesian update.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from agents.sentiment_agent import SentimentSignal


@dataclass
class CalibrationResult:
    prior: float            # market implied probability
    likelihood_ratio: float # sentiment-based update factor
    posterior: float        # calibrated probability
    sentiment_weight: float
    confidence: float


def _logit(p: float) -> float:
    p = max(0.001, min(0.999, p))
    return math.log(p / (1 - p))


def _sigmoid(x: float) -> float:
    return 1.0 / (1.0 + math.exp(-x))


class ProbabilityModel:
    """
    Bayesian update: posterior = sigmoid(logit(prior) + sentiment_adjustment)

    sentiment_adjustment is scaled by signal confidence and a dampening
    factor to avoid over-updating on noisy social data.
    """

    SENTIMENT_SCALE = 0.8   # max logit units sentiment can shift prior
    CONFIDENCE_FLOOR = 0.3  # below this confidence, sentiment is ignored

    def calibrate(
        self,
        market_price: float,
        sentiment: SentimentSignal,
        base_rate: float | None = None,
    ) -> CalibrationResult:
        prior = base_rate if base_rate is not None else market_price
        prior = max(0.01, min(0.99, prior))

        if sentiment.confidence < self.CONFIDENCE_FLOOR:
            return CalibrationResult(
                prior=prior,
                likelihood_ratio=1.0,
                posterior=prior,
                sentiment_weight=0.0,
                confidence=sentiment.confidence,
            )

        # Translate composite sentiment (-1..+1) to a logit shift
        sentiment_signal = sentiment.composite_sentiment * self.SENTIMENT_SCALE
        # Weight by confidence
        weight = sentiment.confidence
        adjustment = sentiment_signal * weight

        posterior = _sigmoid(_logit(prior) + adjustment)
        posterior = max(0.01, min(0.99, posterior))

        lr = (posterior / (1 - posterior)) / (prior / (1 - prior))

        return CalibrationResult(
            prior=prior,
            likelihood_ratio=lr,
            posterior=posterior,
            sentiment_weight=weight,
            confidence=sentiment.confidence,
        )

    def blend(
        self,
        market_price: float,
        claude_estimate: float,
        calibrated: float,
        weights: tuple[float, float, float] = (0.2, 0.5, 0.3),
    ) -> float:
        """Weighted blend of three probability signals."""
        total = sum(weights)
        w1, w2, w3 = [w / total for w in weights]
        blended = w1 * market_price + w2 * claude_estimate + w3 * calibrated
        return max(0.01, min(0.99, blended))
