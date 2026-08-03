"""
Multi-agent debate: a bull, a bear, and a risk manager who decides.

This is the guide's first advanced upgrade. It costs three model calls per
symbol instead of one, so it is opt-in — the point is that a setup has to
survive an adversary before it becomes a signal, not that more calls are
better.

The risk manager gets the last word and is told that passing is free.
"""
from __future__ import annotations

import json
from typing import Optional

from src.ai import prompts
from src.ai.client import LLMClient, LLMError
from src.logging_setup import get_logger
from src.models import NewsAssessment, Signal, TechnicalSnapshot

log = get_logger(__name__)


class AgentDebate:
    def __init__(self, llm: LLMClient) -> None:
        self.llm = llm

    def run(
        self, snapshot: TechnicalSnapshot, news: NewsAssessment
    ) -> tuple[Optional[Signal], dict[str, str]]:
        """Returns (signal or None, transcript). Transcript is kept for the journal."""
        payload = json.dumps(snapshot.to_prompt_dict(), indent=2)
        news_block = (
            f"Impact {news.impact}, bias {news.direction_bias}. {news.summary} "
            f"Catalysts: {', '.join(news.catalysts) or 'none'}."
        )
        transcript: dict[str, str] = {}

        try:
            bull = self.llm.complete(
                prompts.DEBATE_SYSTEM["bull"],
                f"Data for {snapshot.symbol}:\n{payload}\n\nNews: {news_block}\n\n"
                "Make the bull case. Cite levels.",
                effort="medium",
                max_tokens=2048,
            )
            transcript["bull"] = bull.text

            bear = self.llm.complete(
                prompts.DEBATE_SYSTEM["bear"],
                f"Data for {snapshot.symbol}:\n{payload}\n\nNews: {news_block}\n\n"
                f"The bull argues:\n{bull.text}\n\nMake the bear case. Cite levels.",
                effort="medium",
                max_tokens=2048,
            )
            transcript["bear"] = bear.text

            verdict = self.llm.complete(
                prompts.DEBATE_SYSTEM["risk"],
                f"Data for {snapshot.symbol}:\n{payload}\n\n"
                f"News: {news_block}\n\n"
                f"BULL CASE:\n{bull.text}\n\n"
                f"BEAR CASE:\n{bear.text}\n\n"
                "Decide. Passing is free; being wrong is not.",
                schema=prompts.DEBATE_VERDICT_SCHEMA,
            )
        except LLMError as exc:
            log.warning("debate.failed", symbol=snapshot.symbol, error=str(exc))
            return None, transcript

        data = verdict.parsed or {}
        transcript["risk_manager"] = data.get("reasoning", verdict.text)
        transcript["strongest_counterargument"] = data.get("strongest_counterargument", "")

        if data.get("verdict") != "take":
            log.info(
                "debate.pass",
                symbol=snapshot.symbol,
                reason=(data.get("reasoning") or "")[:160],
            )
            return None, transcript

        # Reuse the analyst's validation so a debate signal is held to exactly
        # the same standard as a single-shot one.
        from src.ai.analyst import Analyst  # local import avoids a cycle

        setup = {
            "has_setup": True,
            "direction": data.get("direction"),
            "entry": data.get("entry"),
            "stop": data.get("stop"),
            "target": data.get("target"),
            "confidence": data.get("confidence"),
            "reasoning": data.get("reasoning", ""),
            "invalidation": data.get("invalidation", []),
            "risks": data.get("strongest_counterargument", ""),
            "no_trade_reason": "",
        }
        signal = Analyst.parse_setup(setup, snapshot, news, "swing", source="debate")
        return signal, transcript
