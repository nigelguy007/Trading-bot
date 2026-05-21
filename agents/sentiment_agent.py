"""
Sentiment / Research Swarm Agent
Aggregates signal from Twitter/X, Reddit and news, then asks Claude to
classify bullish/bearish narrative and compare against market odds.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import anthropic
import structlog

from config.settings import settings

log = structlog.get_logger(__name__)


@dataclass
class SentimentSignal:
    market_id: str
    question: str
    market_yes_prob: float
    twitter_sentiment: float    # -1 to +1
    reddit_sentiment: float     # -1 to +1
    news_sentiment: float       # -1 to +1
    composite_sentiment: float  # weighted average
    narrative_bias: str         # "bullish" | "bearish" | "neutral"
    crowd_estimate: float       # crowd's implied probability (0-1)
    narrative_vs_market_delta: float  # crowd_estimate - market_yes_prob
    confidence: float           # 0-1
    key_signals: list[str] = field(default_factory=list)
    raw_posts: list[dict] = field(default_factory=list)


SENTIMENT_SYSTEM_PROMPT = """You are a financial sentiment analyst specialising in prediction markets.
You receive raw social media posts and news about a market question.

Your tasks:
1. Classify each source as bullish (YES likely) or bearish (NO likely) or neutral.
2. Estimate the crowd's implied probability that the event resolves YES (0.0-1.0).
3. Identify the 3 strongest signals driving the narrative.
4. Compare with the current market price and detect mispricing direction.

Be concise and evidence-based. Ignore noise and low-quality posts.

Respond ONLY with valid JSON:
{
  "composite_sentiment": <-1.0 to 1.0>,
  "narrative_bias": "bullish|bearish|neutral",
  "crowd_estimate": <0.0-1.0>,
  "confidence": <0.0-1.0>,
  "key_signals": ["signal1", "signal2", "signal3"],
  "reasoning": "..."
}"""


class SentimentAgent:
    def __init__(self) -> None:
        self.client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
        self.model = "claude-sonnet-4-6"

    def _score_posts_locally(self, posts: list[dict]) -> float:
        """Fast VADER-like heuristic for sentiment (-1 to +1) before Claude call."""
        try:
            from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer
            analyzer = SentimentIntensityAnalyzer()
            scores = []
            for p in posts:
                text = p.get("text", p.get("body", p.get("title", "")))
                if text:
                    scores.append(analyzer.polarity_scores(text)["compound"])
            return sum(scores) / len(scores) if scores else 0.0
        except ImportError:
            return 0.0

    def analyse(
        self,
        market_id: str,
        question: str,
        market_yes_prob: float,
        twitter_posts: list[dict[str, Any]],
        reddit_posts: list[dict[str, Any]],
        news_items: list[dict[str, Any]],
    ) -> SentimentSignal:
        twitter_score = self._score_posts_locally(twitter_posts)
        reddit_score = self._score_posts_locally(reddit_posts)
        news_score = self._score_posts_locally(news_items)

        all_posts = (
            [{"source": "twitter", **p} for p in twitter_posts[:15]]
            + [{"source": "reddit", **p} for p in reddit_posts[:10]]
            + [{"source": "news", **p} for p in news_items[:10]]
        )

        prompt_data = {
            "question": question,
            "market_yes_price": market_yes_prob,
            "posts": [
                {
                    "source": p.get("source"),
                    "text": (p.get("text") or p.get("body") or p.get("title") or "")[:300],
                    "engagement": p.get("likes", p.get("score", 0)),
                }
                for p in all_posts
            ],
        }

        log.info("sentiment.claude_call", market_id=market_id, posts=len(all_posts))

        response = self.client.messages.create(
            model=self.model,
            max_tokens=1024,
            system=SENTIMENT_SYSTEM_PROMPT,
            messages=[
                {
                    "role": "user",
                    "content": (
                        f"Analyse sentiment for this prediction market:\n\n"
                        f"{json.dumps(prompt_data, indent=2)}"
                    ),
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
            log.warning("sentiment.json_parse_error", raw=raw_text[:200])
            parsed = {
                "composite_sentiment": 0.0,
                "narrative_bias": "neutral",
                "crowd_estimate": market_yes_prob,
                "confidence": 0.3,
                "key_signals": [],
                "reasoning": raw_text,
            }

        crowd_estimate = float(parsed.get("crowd_estimate", market_yes_prob))
        composite = float(parsed.get("composite_sentiment", 0.0))

        return SentimentSignal(
            market_id=market_id,
            question=question,
            market_yes_prob=market_yes_prob,
            twitter_sentiment=twitter_score,
            reddit_sentiment=reddit_score,
            news_sentiment=news_score,
            composite_sentiment=composite,
            narrative_bias=parsed.get("narrative_bias", "neutral"),
            crowd_estimate=crowd_estimate,
            narrative_vs_market_delta=crowd_estimate - market_yes_prob,
            confidence=float(parsed.get("confidence", 0.5)),
            key_signals=parsed.get("key_signals", []),
            raw_posts=all_posts,
        )
