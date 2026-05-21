"""
Polymarket CLOB (Central Limit Order Book) API client.
Docs: https://docs.polymarket.com
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import structlog
from tenacity import retry, stop_after_attempt, wait_exponential

from config.settings import settings

log = structlog.get_logger(__name__)

CLOB_BASE = settings.polymarket_base_url


class PolymarketService:
    def __init__(self) -> None:
        self._client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=CLOB_BASE,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {settings.polymarket_api_key}",
                },
                timeout=15.0,
            )
        return self._client

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=10))
    async def get_markets(
        self,
        limit: int = 100,
        active: bool = True,
        min_volume: float = 5000.0,
    ) -> list[dict[str, Any]]:
        client = await self._get_client()
        params: dict[str, Any] = {
            "limit": limit,
            "active": str(active).lower(),
        }
        response = await client.get("/markets", params=params)
        response.raise_for_status()
        data = response.json()

        markets = []
        for m in data.get("data", data if isinstance(data, list) else []):
            volume = float(m.get("volume", 0))
            if volume < min_volume:
                continue

            tokens = m.get("tokens", [])
            yes_price = 0.5
            no_price = 0.5
            if tokens:
                for t in tokens:
                    if t.get("outcome", "").lower() == "yes":
                        yes_price = float(t.get("price", 0.5))
                    elif t.get("outcome", "").lower() == "no":
                        no_price = float(t.get("price", 0.5))

            markets.append(
                {
                    "id": m.get("condition_id", m.get("id", "")),
                    "platform": "polymarket",
                    "question": m.get("question", ""),
                    "yes_price": yes_price,
                    "no_price": no_price,
                    "volume_24h": volume,
                    "liquidity": float(m.get("liquidity", 0)),
                    "time_to_resolution_days": self._days_to_resolution(
                        m.get("end_date_iso") or m.get("game_start_time")
                    ),
                    "_raw": m,
                }
            )

        log.info("polymarket.markets_fetched", count=len(markets))
        return markets

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=10))
    async def get_orderbook(self, token_id: str) -> dict[str, Any]:
        client = await self._get_client()
        response = await client.get(f"/book", params={"token_id": token_id})
        response.raise_for_status()
        return response.json()

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=10))
    async def place_order(
        self,
        market_id: str,
        side: str,
        size: float,
        price: float,
    ) -> dict[str, Any]:
        client = await self._get_client()
        payload = {
            "market": market_id,
            "side": side.upper(),
            "price": str(round(price, 4)),
            "size": str(round(size, 2)),
            "type": "LIMIT",
        }
        response = await client.post("/order", json=payload)
        response.raise_for_status()
        return response.json()

    def _days_to_resolution(self, date_str: str | None) -> float:
        if not date_str:
            return 999.0
        from datetime import datetime, timezone
        try:
            end = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
            now = datetime.now(timezone.utc)
            return max(0.0, (end - now).total_seconds() / 86400)
        except Exception:
            return 999.0

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()
