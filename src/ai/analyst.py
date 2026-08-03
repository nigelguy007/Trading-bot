"""
The analyst: turns market data + headlines into a trade idea, or into nothing.

"Or into nothing" is the important half. Every setup call can return None, and
the prompts are written so that "no trade" is the easy answer rather than the
one the model has to argue for.
"""
from __future__ import annotations

import json
from typing import Optional

from src.ai import prompts
from src.ai.client import LLMClient, LLMError
from src.config import Settings
from src.logging_setup import get_logger
from src.models import (
    NewsAssessment,
    NewsItem,
    Position,
    Signal,
    TechnicalSnapshot,
)

log = get_logger(__name__)


class Analyst:
    def __init__(self, llm: LLMClient, settings: Settings) -> None:
        self.llm = llm
        self.settings = settings

    # ------------------------------------------------------------------ #
    # §6 — Market Analysis
    # ------------------------------------------------------------------ #
    def market_analysis(self, snapshot: TechnicalSnapshot) -> str:
        response = self.llm.complete(
            prompts.ANALYST_SYSTEM,
            prompts.USER_MARKET_ANALYSIS.format(
                ticker=snapshot.symbol,
                data=json.dumps(snapshot.to_prompt_dict(), indent=2),
            ),
        )
        return response.text if response.ok else ""

    # ------------------------------------------------------------------ #
    # §6 — News Analysis
    # ------------------------------------------------------------------ #
    def news_analysis(self, symbol: str, headlines: list[NewsItem]) -> NewsAssessment:
        if not headlines:
            return NewsAssessment(summary="No headlines available.", headline_count=0)

        rendered = "\n".join(f"- {h.headline()}: {h.description}" for h in headlines)
        try:
            response = self.llm.complete(
                prompts.ANALYST_SYSTEM,
                prompts.USER_NEWS_ANALYSIS.format(headlines=rendered, ticker=symbol),
                schema=prompts.NEWS_SCHEMA,
            )
        except LLMError as exc:
            log.warning("analyst.news_failed", symbol=symbol, error=str(exc))
            return NewsAssessment(summary="News analysis unavailable.", headline_count=len(headlines))

        data = response.parsed or {}
        return NewsAssessment(
            impact=data.get("impact", "low"),
            summary=data.get("summary", ""),
            catalysts=data.get("catalysts", []) or [],
            noise=data.get("noise", []) or [],
            direction_bias=data.get("direction_bias", "neutral"),
            headline_count=len(headlines),
        )

    # ------------------------------------------------------------------ #
    # §6 — Swing Trading / Day Trading
    # ------------------------------------------------------------------ #
    def find_setup(
        self,
        snapshot: TechnicalSnapshot,
        news: NewsAssessment,
        timeframe: str = "swing",
    ) -> Optional[Signal]:
        template = (
            prompts.USER_SWING_TRADING if timeframe == "swing" else prompts.USER_DAY_TRADING
        )
        news_block = (
            f"Impact: {news.impact} | Bias: {news.direction_bias}\n"
            f"{news.summary}\n"
            f"Catalysts: {', '.join(news.catalysts) or 'none'}\n"
            f"Noise (ignore): {', '.join(news.noise) or 'none'}"
        )

        try:
            response = self.llm.complete(
                prompts.ANALYST_SYSTEM,
                template.format(
                    ticker=snapshot.symbol,
                    data=json.dumps(snapshot.to_prompt_dict(), indent=2),
                    news=news_block,
                ),
                schema=prompts.SETUP_SCHEMA,
            )
        except LLMError as exc:
            log.warning("analyst.setup_failed", symbol=snapshot.symbol, error=str(exc))
            return None

        if response.refused:
            log.warning("analyst.setup_refused", symbol=snapshot.symbol)
            return None

        return self.parse_setup(response.parsed, snapshot, news, timeframe, source="ai")

    @staticmethod
    def parse_setup(
        data: Optional[dict],
        snapshot: TechnicalSnapshot,
        news: NewsAssessment,
        timeframe: str,
        source: str,
    ) -> Optional[Signal]:
        """Validate a model-produced setup into a Signal, or reject it."""
        if not data:
            return None

        if not data.get("has_setup"):
            log.info(
                "analyst.no_setup",
                symbol=snapshot.symbol,
                reason=(data.get("no_trade_reason") or "no clean setup")[:160],
            )
            return None

        direction = data.get("direction")
        if direction not in ("long", "short"):
            log.info("analyst.no_setup", symbol=snapshot.symbol, reason="no direction given")
            return None

        try:
            entry = float(data.get("entry") or 0)
            stop = float(data.get("stop") or 0)
            target = float(data.get("target") or 0)
            confidence = float(data.get("confidence") or 0)
        except (TypeError, ValueError):
            log.warning("analyst.setup_unparseable", symbol=snapshot.symbol)
            return None

        if min(entry, stop, target) <= 0:
            log.warning("analyst.setup_incomplete", symbol=snapshot.symbol,
                        entry=entry, stop=stop, target=target)
            return None

        signal = Signal(
            symbol=snapshot.symbol,
            direction=direction,
            entry=entry,
            stop=stop,
            target=target,
            confidence=max(0.0, min(1.0, confidence)),
            reasoning=data.get("reasoning", ""),
            timeframe="swing" if timeframe == "swing" else "intraday",
            source=source,
            invalidation=data.get("invalidation", []) or [],
            news_impact=news.impact,
            risk_notes=data.get("risks", ""),
            asset_class="crypto" if "/" in snapshot.symbol else "equity",
        )

        # A stop on the wrong side of entry is not a rounding error, it is a
        # broken plan. Reject rather than "fix" it.
        if not signal.stop_is_coherent():
            log.warning(
                "analyst.incoherent_levels",
                symbol=signal.symbol,
                direction=direction,
                entry=entry,
                stop=stop,
                target=target,
            )
            return None

        return signal

    # ------------------------------------------------------------------ #
    # §6 — Risk Analysis / Portfolio Review / Chart Breakdown
    # ------------------------------------------------------------------ #
    def risk_analysis(self, signal: Signal, equity: float, positions: list[Position]) -> str:
        trade = (
            f"{signal.symbol} {signal.direction} entry {signal.entry} "
            f"stop {signal.stop} target {signal.target} "
            f"(R:R {signal.risk_reward:.2f})"
        )
        pos = ", ".join(f"{p.symbol} {p.qty:g}@{p.avg_entry:.2f}" for p in positions) or "none"
        response = self.llm.complete(
            prompts.RISK_SYSTEM,
            prompts.USER_RISK_ANALYSIS.format(
                trade=trade, equity=f"{equity:,.2f}", positions=pos
            ),
            effort="medium",
        )
        return response.text if response.ok else ""

    def portfolio_review(self, positions: list[Position], equity: float, open_risk: float) -> str:
        if not positions:
            return "No open positions."
        rendered = ", ".join(
            f"{p.symbol} {p.qty:g} shares @ {p.avg_entry:.2f} "
            f"(now {p.market_price:.2f}, P&L {p.unrealized_pnl:+.2f})"
            for p in positions
        )
        response = self.llm.complete(
            prompts.RISK_SYSTEM,
            prompts.USER_PORTFOLIO_REVIEW.format(
                positions=rendered,
                equity=f"{equity:,.2f}",
                open_risk=f"{open_risk:,.2f}",
            ),
            effort="medium",
        )
        return response.text if response.ok else ""

    def chart_breakdown(self, snapshot: TechnicalSnapshot) -> str:
        response = self.llm.complete(
            prompts.ANALYST_SYSTEM,
            prompts.USER_CHART_BREAKDOWN.format(
                ticker=snapshot.symbol,
                data=json.dumps(snapshot.to_prompt_dict(), indent=2),
            ),
        )
        return response.text if response.ok else ""

    def position_sizing_note(self, equity: float, entry: float, stop: float) -> str:
        """The guide's Position Sizing prompt. The bot sizes arithmetically in
        src/sizing.py — this is the second opinion, for the alert body."""
        response = self.llm.complete(
            prompts.RISK_SYSTEM,
            prompts.USER_POSITION_SIZING.format(
                equity=f"{equity:,.2f}",
                risk_pct=self.settings.risk_per_trade_pct,
                entry=entry,
                stop=stop,
            ),
            effort="low",
            max_tokens=1024,
        )
        return response.text if response.ok else ""
