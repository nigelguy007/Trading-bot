"""
Risk Management Agent
Enforces hard limits on position size, daily drawdown, and portfolio exposure.
Applies fractional Kelly sizing and returns a safe trade size.
"""
from __future__ import annotations

import json
from dataclasses import dataclass

import anthropic
import structlog

from agents.edge_detector import EdgeResult
from config.settings import settings

log = structlog.get_logger(__name__)


@dataclass
class PortfolioState:
    bankroll: float
    daily_pnl: float
    open_positions_value: float
    open_position_count: int
    daily_trades: int


@dataclass
class RiskDecision:
    approved: bool
    position_size_usd: float
    kelly_fraction_used: float
    rejection_reason: str | None
    risk_notes: str


RISK_SYSTEM_PROMPT = """You are a risk management agent for a prediction market trading system.
Your job is to review a proposed trade and the current portfolio state, then decide:
1. Is the trade safe to execute?
2. What is the appropriate position size in USD?

Hard rules (NEVER violate):
- Daily loss limit: ${max_daily_loss} USD
- Max single position: ${max_position} USD
- Max total portfolio exposure: ${max_exposure} USD
- Minimum confidence score: {min_confidence}
- Minimum edge: {min_edge}

Apply half-Kelly sizing as a default (multiply Kelly fraction by 0.5 for safety).

Respond ONLY with valid JSON:
{{
  "approved": true|false,
  "position_size_usd": <number>,
  "kelly_fraction_used": <0.0-1.0>,
  "rejection_reason": null|"string",
  "risk_notes": "..."
}}"""


class RiskAgent:
    def __init__(self) -> None:
        self.client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
        self.model = "claude-sonnet-4-6"

    def _hard_checks(
        self, edge: EdgeResult, portfolio: PortfolioState
    ) -> str | None:
        """Return rejection reason string or None if all checks pass."""
        if not edge.has_edge:
            return "No statistically significant edge detected."
        if edge.confidence < settings.min_confidence_score:
            return f"Confidence {edge.confidence:.2f} below threshold {settings.min_confidence_score}."
        if portfolio.daily_pnl <= -settings.max_daily_loss_usd:
            return f"Daily loss limit of ${settings.max_daily_loss_usd} reached."
        remaining_exposure = (
            settings.max_portfolio_exposure_usd - portfolio.open_positions_value
        )
        if remaining_exposure <= 0:
            return "Maximum portfolio exposure reached."
        return None

    def evaluate(
        self,
        edge: EdgeResult,
        portfolio: PortfolioState,
    ) -> RiskDecision:
        hard_block = self._hard_checks(edge, portfolio)
        if hard_block:
            return RiskDecision(
                approved=False,
                position_size_usd=0.0,
                kelly_fraction_used=0.0,
                rejection_reason=hard_block,
                risk_notes="Blocked by hard risk rules.",
            )

        system = RISK_SYSTEM_PROMPT.format(
            max_daily_loss=settings.max_daily_loss_usd,
            max_position=settings.max_position_size_usd,
            max_exposure=settings.max_portfolio_exposure_usd,
            min_confidence=settings.min_confidence_score,
            min_edge=settings.min_edge_threshold,
        )

        prompt_data = {
            "market_id": edge.market_id,
            "direction": edge.direction,
            "edge": round(edge.edge, 4),
            "expected_value": round(edge.expected_value, 4),
            "kelly_fraction": round(edge.kelly_fraction, 4),
            "model_confidence": round(edge.confidence, 4),
            "portfolio": {
                "bankroll": portfolio.bankroll,
                "daily_pnl": portfolio.daily_pnl,
                "open_positions_value": portfolio.open_positions_value,
                "open_position_count": portfolio.open_position_count,
                "daily_trades": portfolio.daily_trades,
            },
        }

        log.info("risk.claude_call", market_id=edge.market_id)

        response = self.client.messages.create(
            model=self.model,
            max_tokens=512,
            system=system,
            messages=[
                {
                    "role": "user",
                    "content": f"Evaluate this trade:\n\n{json.dumps(prompt_data, indent=2)}",
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
            log.warning("risk.json_parse_error", raw=raw_text[:200])
            return RiskDecision(
                approved=False,
                position_size_usd=0.0,
                kelly_fraction_used=0.0,
                rejection_reason="Risk agent parse error.",
                risk_notes=raw_text,
            )

        # Enforce hard caps regardless of Claude's output
        pos_size = min(
            float(parsed.get("position_size_usd", 0.0)),
            settings.max_position_size_usd,
            settings.max_portfolio_exposure_usd - portfolio.open_positions_value,
        )
        pos_size = max(pos_size, 0.0)

        return RiskDecision(
            approved=bool(parsed.get("approved", False)) and pos_size > 0,
            position_size_usd=round(pos_size, 2),
            kelly_fraction_used=float(parsed.get("kelly_fraction_used", 0.0)),
            rejection_reason=parsed.get("rejection_reason"),
            risk_notes=parsed.get("risk_notes", ""),
        )
