"""
Execution Agent
Validates final trade parameters, then routes to Polymarket or Kalshi API.
"""
from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import structlog

from agents.edge_detector import EdgeResult
from agents.risk_agent import RiskDecision
from config.settings import settings

log = structlog.get_logger(__name__)


@dataclass
class TradeOrder:
    order_id: str
    market_id: str
    platform: str
    direction: str       # "yes" | "no"
    size_usd: float
    limit_price: float
    model_prob: float
    edge: float
    confidence: float
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class TradeResult:
    order_id: str
    market_id: str
    platform: str
    status: str          # "filled" | "partial" | "rejected" | "error"
    filled_size_usd: float
    avg_fill_price: float
    fee_usd: float
    pnl_unrealized: float
    raw_response: dict = field(default_factory=dict)
    error_message: str | None = None


class ExecutionAgent:
    def __init__(
        self,
        polymarket_service: Any = None,
        kalshi_service: Any = None,
    ) -> None:
        self.polymarket = polymarket_service
        self.kalshi = kalshi_service

    def _build_order(
        self,
        edge: EdgeResult,
        risk: RiskDecision,
        platform: str,
        market_yes_price: float,
    ) -> TradeOrder:
        if edge.direction == "yes":
            limit_price = market_yes_price * 1.01  # 1% slippage tolerance
        else:
            limit_price = (1 - market_yes_price) * 1.01

        limit_price = min(limit_price, 0.99)

        return TradeOrder(
            order_id=str(uuid.uuid4()),
            market_id=edge.market_id,
            platform=platform,
            direction=edge.direction,
            size_usd=risk.position_size_usd,
            limit_price=round(limit_price, 4),
            model_prob=edge.model_yes_prob,
            edge=edge.edge,
            confidence=edge.confidence,
        )

    async def execute(
        self,
        edge: EdgeResult,
        risk: RiskDecision,
        platform: str,
        market_yes_price: float,
        dry_run: bool = True,
    ) -> TradeResult:
        if not risk.approved:
            return TradeResult(
                order_id=str(uuid.uuid4()),
                market_id=edge.market_id,
                platform=platform,
                status="rejected",
                filled_size_usd=0.0,
                avg_fill_price=0.0,
                fee_usd=0.0,
                pnl_unrealized=0.0,
                error_message=risk.rejection_reason,
            )

        order = self._build_order(edge, risk, platform, market_yes_price)

        if dry_run:
            log.info(
                "execution.dry_run",
                order_id=order.order_id,
                market_id=order.market_id,
                platform=platform,
                direction=order.direction,
                size_usd=order.size_usd,
                limit_price=order.limit_price,
            )
            return TradeResult(
                order_id=order.order_id,
                market_id=order.market_id,
                platform=platform,
                status="filled",
                filled_size_usd=order.size_usd,
                avg_fill_price=order.limit_price,
                fee_usd=round(order.size_usd * 0.001, 4),
                pnl_unrealized=0.0,
                raw_response={"dry_run": True},
            )

        try:
            if platform == "polymarket" and self.polymarket:
                result = await self._execute_polymarket(order)
            elif platform == "kalshi" and self.kalshi:
                result = await self._execute_kalshi(order)
            else:
                raise ValueError(f"No service configured for platform: {platform}")
            return result
        except Exception as exc:
            log.error("execution.error", order_id=order.order_id, error=str(exc))
            return TradeResult(
                order_id=order.order_id,
                market_id=order.market_id,
                platform=platform,
                status="error",
                filled_size_usd=0.0,
                avg_fill_price=0.0,
                fee_usd=0.0,
                pnl_unrealized=0.0,
                error_message=str(exc),
            )

    async def _execute_polymarket(self, order: TradeOrder) -> TradeResult:
        response = await self.polymarket.place_order(
            market_id=order.market_id,
            side=order.direction,
            size=order.size_usd,
            price=order.limit_price,
        )
        return TradeResult(
            order_id=order.order_id,
            market_id=order.market_id,
            platform="polymarket",
            status=response.get("status", "unknown"),
            filled_size_usd=float(response.get("size_matched", 0)),
            avg_fill_price=float(response.get("price", order.limit_price)),
            fee_usd=float(response.get("fee", 0)),
            pnl_unrealized=0.0,
            raw_response=response,
        )

    async def _execute_kalshi(self, order: TradeOrder) -> TradeResult:
        response = await self.kalshi.place_order(
            ticker=order.market_id,
            side=order.direction,
            count=int(order.size_usd / 100),  # Kalshi uses contracts
            type="limit",
            yes_price=int(order.limit_price * 100),
        )
        return TradeResult(
            order_id=order.order_id,
            market_id=order.market_id,
            platform="kalshi",
            status=response.get("status", "unknown"),
            filled_size_usd=float(response.get("filled_cost", 0)),
            avg_fill_price=float(response.get("yes_price", order.limit_price * 100)) / 100,
            fee_usd=float(response.get("fee", 0)),
            pnl_unrealized=0.0,
            raw_response=response,
        )
