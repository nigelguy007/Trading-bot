"""
Headlines, so the AI sees context rather than price alone.

Cached hard: NewsAPI's free tier is easy to rate-limit out of, and headlines
for a ticker don't meaningfully change every fifteen minutes.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx

from src.config import Settings
from src.data.cache import TTLCache
from src.logging_setup import get_logger
from src.models import NewsItem

log = get_logger(__name__)

NEWSAPI_URL = "https://newsapi.org/v2/everything"

# Ticker -> company name, so the query catches articles that never print the
# symbol. Unknown tickers fall back to the bare symbol.
COMPANY_NAMES = {
    "AAPL": "Apple", "MSFT": "Microsoft", "NVDA": "Nvidia", "AMD": "AMD",
    "TSLA": "Tesla", "AMZN": "Amazon", "GOOGL": "Google", "META": "Meta",
    "NFLX": "Netflix", "SPY": "S&P 500", "QQQ": "Nasdaq 100",
    "BTC/USD": "Bitcoin", "ETH/USD": "Ethereum",
}


class NewsClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.cache = TTLCache(settings.data_path, "news")
        self._client = httpx.Client(timeout=15.0)

    def close(self) -> None:
        self._client.close()

    @property
    def enabled(self) -> bool:
        return bool(self.settings.news_api_key)

    def get_headlines(self, symbol: str, limit: int = 10) -> list[NewsItem]:
        if not self.enabled:
            return []

        key = f"news|{symbol}|{limit}"
        raw = self.cache.get(key, self.settings.news_cache_ttl)
        if raw is None:
            raw = self._fetch(symbol, limit)
            if raw is not None:
                self.cache.set(key, raw)
        return [
            NewsItem(
                title=a.get("title") or "",
                source=(a.get("source") or {}).get("name", "unknown"),
                url=a.get("url") or "",
                published_at=a.get("publishedAt") or "",
                description=(a.get("description") or "")[:400],
            )
            for a in (raw or [])
            if a.get("title")
        ]

    def _fetch(self, symbol: str, limit: int) -> list[dict] | None:
        name = COMPANY_NAMES.get(symbol.upper(), symbol)
        query = f'"{name}"' if name != symbol else symbol
        params = {
            "q": query,
            "language": "en",
            "sortBy": "publishedAt",
            "pageSize": min(limit, 50),
            "from": (datetime.now(timezone.utc) - timedelta(days=3)).date().isoformat(),
            "apiKey": self.settings.news_api_key,
        }
        try:
            resp = self._client.get(NEWSAPI_URL, params=params)
        except httpx.HTTPError as exc:
            log.warning("news.network_error", symbol=symbol, error=str(exc))
            return None

        if resp.status_code == 429:
            # Serve stale rather than nothing, and back off next cycle.
            log.warning("news.rate_limited", symbol=symbol, hint="raise NEWS_CACHE_TTL")
            return None
        if resp.status_code == 401:
            log.error("news.unauthorized", hint="check NEWS_API_KEY in .env")
            return None
        if resp.status_code >= 400:
            log.warning("news.http_error", status=resp.status_code, body=resp.text[:200])
            return None

        return resp.json().get("articles", [])[:limit]
