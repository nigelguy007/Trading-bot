"""
Edge Detection Engine
Calculates whether market odds are statistically mispriced using Kelly Criterion,
implied probability comparison, and Claude's probabilistic reasoning.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass

import anthropic
import structlog

from agents.market_scanner import MarketOpportunity
from agents.sentiment_agent import SentimentSignal
from config.settings import settings

log = structlog.get_logger(__name__)


@dataclass
class EdgeResult:
    market_id: str
    market_yes_prob: float          # What the market thinks
    model_yes_prob: float           # What our model thinks
    edge: float                     # model_prob - market_prob
    expected_value: float           # EV of a YES bet
    kelly_fraction: float           # Optimal bet size as fraction of bankroll
    direction: str                  # "yes" | "no" | "pass"
    confidence: float               # 0-1
    has_edge: bool
    reasoning: str


EDGE_SYSTEM_PROMPT = """You are a probabilistic reasoning engine for prediction markets.
Given market data and sentiment analysis, your task is to estimate the TRUE probability
of the event resolving YES, independent of the market price.

Use Bayesian reasoning:
- Start from base rates for similar events.
- Update based on recent news and sentiment signals.
- Apply scepticism: public sentiment often overreacts.
- Consider time remaining and resolution mechanics.

Return ONLY valid JSON:
{
  "model_yes_prob": <0.0-1.0>,
  "confidence": <0.0-1.0>,
  "reasoning": "...",
  "key_factors": ["factor1", "factor2"]
}"""


def _kelly_fraction(prob: float, market_prob: float, max_kelly: float = 0.25) -> float:
    """
    Full Kelly formula for binary prediction markets.
    Capped at max_kelly to limit variance.
    """
    if market_prob <= 0 or market_prob >= 1:
        return 0.0
    # Odds received if correct: (1/market_prob - 1)
    b = (1.0 / market_prob) - 1.0
    q = 1.0 - prob
    numerator = b * prob - q
    if numerator <= 0:
        return 0.0
    fraction = numerator / b
    return min(fraction, max_kelly)


def _expected_value(prob: float, market_prob: float) -> float:
    """EV per dollar staked on YES."""
    payout = 1.0 / market_prob  # $1 bet returns $1/market_prob if correct
    return prob * payout - 1.0


class EdgeDetectorAgent:
    def __init__(self) -> None:
        self.client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
        self.model = "claude-sonnet-4-6"

    def detect(
        self,
        opportunity: MarketOpportunity,
        sentiment: SentimentSignal,
    ) -> EdgeResult:
        prompt_data = {
            "question": opportunity.question,
            "platform": opportunity.platform,
            "market_yes_price": opportunity.yes_price,
            "market_no_price": opportunity.no_price,
            "volume_24h_usd": opportunity.volume_24h,
            "liquidity_usd": opportunity.liquidity,
            "time_to_resolution_days": opportunity.time_to_resolution_days,
            "sentiment": {
                "composite": sentiment.composite_sentiment,
                "narrative_bias": sentiment.narrative_bias,
                "crowd_estimate": sentiment.crowd_estimate,
                "confidence": sentiment.confidence,
                "key_signals": sentiment.key_signals,
            },
        }

        log.info("edge.claude_call", market_id=opportunity.market_id)

        response = self.client.messages.create(
            model=self.model,
            max_tokens=1024,
            system=EDGE_SYSTEM_PROMPT,
            messages=[
                {
                    "role": "user",
                    "content": f"Estimate true probability:\n\n{json.dumps(prompt_data, indent=2)}",
                }
            ],
        )

        raw_text = response.content[0].text.strip()
        if raw_text.startswith("```"):
            raw_text = raw_text.split("```")[1]
            if raw_text.startswith("json"):
                raw_text = raw_text[4:]

        try:
            parsed = json.loads(raw_text)
        except json.JSONDecodeError:
            log.warning("edge.json_parse_error", raw=raw_text[:200])
            parsed = {
                "model_yes_prob": opportunity.yes_price,
                "confidence": 0.3,
                "reasoning": raw_text,
                "key_factors": [],
            }

        model_prob = float(parsed.get("model_yes_prob", opportunity.yes_price))
        model_prob = max(0.01, min(0.99, model_prob))
        confidence = float(parsed.get("confidence", 0.5))

        yes_edge = model_prob - opportunity.yes_price
        no_edge = (1 - model_prob) - opportunity.no_price

        # Pick best direction
        if abs(yes_edge) >= abs(no_edge) and yes_edge > settings.min_edge_threshold:
            direction = "yes"
            edge = yes_edge
            kf = _kelly_fraction(model_prob, opportunity.yes_price)
            ev = _expected_value(model_prob, opportunity.yes_price)
        elif no_edge > settings.min_edge_threshold:
            direction = "no"
            edge = no_edge
            no_model_prob = 1.0 - model_prob
            kf = _kelly_fraction(no_model_prob, opportunity.no_price)
            ev = _expected_value(no_model_prob, opportunity.no_price)
        else:
            direction = "pass"
            edge = max(yes_edge, no_edge)
            kf = 0.0
            ev = 0.0

        has_edge = (
            direction != "pass"
            and abs(edge) >= settings.min_edge_threshold
            and confidence >= settings.min_confidence_score
        )

        return EdgeResult(
            market_id=opportunity.market_id,
            market_yes_prob=opportunity.yes_price,
            model_yes_prob=model_prob,
            edge=edge,
            expected_value=ev,
            kelly_fraction=kf,
            direction=direction,
            confidence=confidence,
            has_edge=has_edge,
            reasoning=parsed.get("reasoning", ""),
        )
