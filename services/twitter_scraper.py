"""
Twitter/X v2 API scraper — searches recent tweets relevant to a market question.
"""
from __future__ import annotations

from typing import Any

import httpx
import structlog
from tenacity import retry, stop_after_attempt, wait_exponential

from config.settings import settings

log = structlog.get_logger(__name__)

TWITTER_V2_BASE = "https://api.twitter.com/2"


class TwitterScraper:
    def __init__(self) -> None:
        self._client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=TWITTER_V2_BASE,
                headers={"Authorization": f"Bearer {settings.twitter_bearer_token}"},
                timeout=15.0,
            )
        return self._client

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=8))
    async def search(
        self,
        query: str,
        max_results: int = 50,
    ) -> list[dict[str, Any]]:
        if not settings.twitter_bearer_token:
            log.warning("twitter.no_token")
            return []

        client = await self._get_client()
        safe_query = f"({query}) lang:en -is:retweet"

        response = await client.get(
            "/tweets/search/recent",
            params={
                "query": safe_query,
                "max_results": min(max_results, 100),
                "tweet.fields": "created_at,public_metrics,author_id",
                "expansions": "author_id",
            },
        )

        if response.status_code == 429:
            log.warning("twitter.rate_limited")
            return []

        response.raise_for_status()
        data = response.json()

        posts = []
        for tweet in data.get("data", []):
            metrics = tweet.get("public_metrics", {})
            posts.append(
                {
                    "id": tweet.get("id"),
                    "text": tweet.get("text", ""),
                    "created_at": tweet.get("created_at"),
                    "likes": metrics.get("like_count", 0),
                    "retweets": metrics.get("retweet_count", 0),
                    "replies": metrics.get("reply_count", 0),
                    "engagement": (
                        metrics.get("like_count", 0)
                        + metrics.get("retweet_count", 0) * 2
                    ),
                }
            )

        posts.sort(key=lambda x: x["engagement"], reverse=True)
        log.info("twitter.posts_fetched", query=query[:50], count=len(posts))
        return posts

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()
