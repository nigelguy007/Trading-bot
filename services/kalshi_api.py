"""
Kalshi REST API v2 client.
Docs: https://trading-api.kalshi.com/trade-api/v2/openapi.json
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import httpx
import structlog
from tenacity import retry, stop_after_attempt, wait_exponential

from config.settings import settings

log = structlog.get_logger(__name__)


class KalshiService:
    def __init__(self) -> None:
        self._client: httpx.AsyncClient | None = None
        self._token: str | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=settings.kalshi_base_url,
                headers={"Content-Type": "application/json"},
                timeout=15.0,
            )
        return self._client

    async def _ensure_auth(self) -> None:
        if self._token:
            return
        client = await self._get_client()
        response = await client.post(
            "/login",
            json={"email": settings.kalshi_email, "password": settings.kalshi_password},
        )
        response.raise_for_status()
        self._token = response.json().get("token")
        client.headers["Authorization"] = f"Bearer {self._token}"

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=10))
    async def get_markets(
        self,
        limit: int = 200,
        status: str = "open",
        min_volume: float = 5000.0,
    ) -> list[dict[str, Any]]:
        await self._ensure_auth()
        client = await self._get_client()

        response = await client.get(
            "/markets",
            params={"limit": limit, "status": status},
        )
        response.raise_for_status()
        data = response.json()

        markets = []
        for m in data.get("markets", []):
            volume = float(m.get("volume", 0))
            if volume < min_volume:
                continue

            yes_price = float(m.get("yes_ask", m.get("last_price", 50))) / 100
            no_price = float(m.get("no_ask", 100 - m.get("last_price", 50))) / 100

            close_time = m.get("close_time") or m.get("expiration_time")
            days = self._days_to_resolution(close_time)

            markets.append(
                {
                    "id": m.get("ticker", ""),
                    "platform": "kalshi",
                    "question": m.get("title", ""),
                    "yes_price": yes_price,
                    "no_price": no_price,
                    "volume_24h": volume,
                    "liquidity": float(m.get("open_interest", 0)),
                    "time_to_resolution_days": days,
                    "_raw": m,
                }
            )

        log.info("kalshi.markets_fetched", count=len(markets))
        return markets

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=10))
    async def place_order(
        self,
        ticker: str,
        side: str,
        count: int,
        type: str = "limit",
        yes_price: int = 50,
    ) -> dict[str, Any]:
        await self._ensure_auth()
        client = await self._get_client()
        payload = {
            "ticker": ticker,
            "action": "buy",
            "side": side,
            "count": count,
            "type": type,
            "yes_price": yes_price,
        }
        response = await client.post("/portfolio/orders", json=payload)
        response.raise_for_status()
        return response.json().get("order", {})

    def _days_to_resolution(self, date_str: str | None) -> float:
        if not date_str:
            return 999.0
        try:
            end = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
            now = datetime.now(timezone.utc)
            return max(0.0, (end - now).total_seconds() / 86400)
        except Exception:
            return 999.0

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()
