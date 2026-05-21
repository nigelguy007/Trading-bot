"""
Reddit scraper — uses the public JSON API (no auth required for read-only).
Searches relevant subreddits for discussion about a market question.
"""
from __future__ import annotations

from typing import Any

import httpx
import structlog
from tenacity import retry, stop_after_attempt, wait_exponential

from config.settings import settings

log = structlog.get_logger(__name__)

SUBREDDITS = [
    "PredictionMarket",
    "Polymarket",
    "Kalshi",
    "worldnews",
    "politics",
    "investing",
]


class RedditScraper:
    def __init__(self) -> None:
        self._client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                headers={"User-Agent": settings.reddit_user_agent},
                timeout=15.0,
                follow_redirects=True,
            )
        return self._client

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=8))
    async def search(
        self,
        query: str,
        subreddits: list[str] | None = None,
        limit: int = 25,
    ) -> list[dict[str, Any]]:
        client = await self._get_client()
        targets = subreddits or SUBREDDITS[:4]
        sr_string = "+".join(targets)

        response = await client.get(
            f"https://www.reddit.com/r/{sr_string}/search.json",
            params={
                "q": query,
                "sort": "relevance",
                "t": "week",
                "limit": limit,
                "restrict_sr": "1",
            },
        )

        if response.status_code in (429, 403):
            log.warning("reddit.blocked", status=response.status_code)
            return []

        response.raise_for_status()
        data = response.json()

        posts = []
        for child in data.get("data", {}).get("children", []):
            post = child.get("data", {})
            posts.append(
                {
                    "id": post.get("id"),
                    "title": post.get("title", ""),
                    "body": post.get("selftext", "")[:500],
                    "subreddit": post.get("subreddit"),
                    "score": post.get("score", 0),
                    "num_comments": post.get("num_comments", 0),
                    "created_utc": post.get("created_utc"),
                    "url": post.get("url"),
                }
            )

        posts.sort(key=lambda x: x["score"], reverse=True)
        log.info("reddit.posts_fetched", query=query[:50], count=len(posts))
        return posts

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()
