"""
Market Scanner Agent
Scans Polymarket and Kalshi for liquid, inefficiently priced opportunities.
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
class MarketOpportunity:
    market_id: str
    platform: str          # "polymarket" | "kalshi"
    question: str
    yes_price: float       # 0-1
    no_price: float        # 0-1
    implied_yes_prob: float
    volume_24h: float      # USD
    liquidity: float       # USD
    spread: float          # yes_price + no_price - 1 (negative = bookmaker edge)
    time_to_resolution_days: float
    raw: dict = field(default_factory=dict)


@dataclass
class ScanResult:
    opportunities: list[MarketOpportunity]
    total_scanned: int
    filters_applied: list[str]
    scanner_reasoning: str


SCANNER_SYSTEM_PROMPT = """You are a prediction market scanner agent. Your job is to analyse raw
market data and identify opportunities worth deeper investigation.

For each market provided:
1. Assess whether implied probabilities look inefficient vs base rates.
2. Flag markets where spread > 0.03 (3 cents over fair value).
3. Prioritise markets with >$10,000 liquidity and >$5,000 24h volume.
4. Consider time-to-resolution: prefer 3-30 day windows.
5. Return a JSON array of market_ids you recommend for further analysis,
   plus a brief reasoning string for each.

Respond ONLY with valid JSON matching this schema:
{
  "recommendations": [
    {"market_id": "...", "reason": "...", "priority": "high|medium|low"}
  ],
  "scanner_notes": "..."
}"""


class MarketScannerAgent:
    def __init__(self) -> None:
        self.client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
        self.model = "claude-sonnet-4-6"

    def _build_filter_pipeline(
        self, markets: list[dict[str, Any]]
    ) -> list[MarketOpportunity]:
        """Apply hard filters before sending to Claude to save tokens."""
        filtered: list[MarketOpportunity] = []

        for m in markets:
            yes_price = float(m.get("yes_price", m.get("last_trade_price", 0.5)))
            no_price = float(m.get("no_price", 1 - yes_price))
            volume = float(m.get("volume_24h", m.get("volume", 0)))
            liquidity = float(m.get("liquidity", m.get("open_interest", 0)))
            spread = yes_price + no_price - 1.0

            # Hard filter: liquidity & volume
            if liquidity < settings.min_liquidity_usd:
                continue
            if volume < 1000:
                continue

            time_days = float(m.get("time_to_resolution_days", 999))
            if time_days > 90:
                continue

            opp = MarketOpportunity(
                market_id=str(m.get("id", m.get("ticker", ""))),
                platform=m.get("platform", "unknown"),
                question=m.get("question", m.get("title", "")),
                yes_price=yes_price,
                no_price=no_price,
                implied_yes_prob=yes_price,
                volume_24h=volume,
                liquidity=liquidity,
                spread=spread,
                time_to_resolution_days=time_days,
                raw=m,
            )
            filtered.append(opp)

        return filtered

    def scan(self, markets: list[dict[str, Any]]) -> ScanResult:
        pre_filtered = self._build_filter_pipeline(markets)

        if not pre_filtered:
            return ScanResult(
                opportunities=[],
                total_scanned=len(markets),
                filters_applied=["liquidity", "volume", "time_to_resolution"],
                scanner_reasoning="No markets passed hard filters.",
            )

        market_summaries = [
            {
                "market_id": o.market_id,
                "platform": o.platform,
                "question": o.question,
                "yes_price": round(o.yes_price, 4),
                "no_price": round(o.no_price, 4),
                "spread": round(o.spread, 4),
                "volume_24h": round(o.volume_24h, 2),
                "liquidity": round(o.liquidity, 2),
                "time_to_resolution_days": round(o.time_to_resolution_days, 1),
            }
            for o in pre_filtered
        ]

        log.info("scanner.claude_call", markets_sent=len(market_summaries))

        response = self.client.messages.create(
            model=self.model,
            max_tokens=2048,
            system=SCANNER_SYSTEM_PROMPT,
            messages=[
                {
                    "role": "user",
                    "content": f"Analyse these prediction markets:\n\n{json.dumps(market_summaries, indent=2)}",
                }
            ],
        )

        raw_text = response.content[0].text.strip()
        # Strip markdown code fences if present
        if raw_text.startswith("```"):
            raw_text = raw_text.split("```")[1]
            if raw_text.startswith("json"):
                raw_text = raw_text[4:]

        try:
            parsed = json.loads(raw_text)
        except json.JSONDecodeError:
            log.warning("scanner.json_parse_error", raw=raw_text[:200])
            parsed = {"recommendations": [], "scanner_notes": raw_text}

        recommended_ids = {r["market_id"] for r in parsed.get("recommendations", [])}
        final = [o for o in pre_filtered if o.market_id in recommended_ids]

        return ScanResult(
            opportunities=final,
            total_scanned=len(markets),
            filters_applied=["liquidity", "volume", "time_to_resolution", "claude_analysis"],
            scanner_reasoning=parsed.get("scanner_notes", ""),
        )
