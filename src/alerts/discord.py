"""Discord alerts via an incoming webhook."""
from __future__ import annotations

import httpx

from src.logging_setup import get_logger

log = get_logger(__name__)

DISCORD_LIMIT = 2000


class DiscordAlerts:
    name = "discord"

    def __init__(self, webhook_url: str) -> None:
        self.webhook_url = webhook_url
        self._client = httpx.Client(timeout=15.0)

    @property
    def enabled(self) -> bool:
        return bool(self.webhook_url)

    def close(self) -> None:
        self._client.close()

    def send(self, text: str, embed: dict | None = None) -> bool:
        if not self.enabled:
            return False

        payload: dict = {"content": f"```\n{text[: DISCORD_LIMIT - 10]}\n```"}
        if embed:
            payload = {"embeds": [embed]}

        try:
            resp = self._client.post(self.webhook_url, json=payload)
        except httpx.HTTPError as exc:
            log.warning("alerts.discord_network_error", error=str(exc))
            return False

        if resp.status_code == 429:
            log.warning("alerts.discord_rate_limited")
            return False
        if resp.status_code >= 400:
            log.warning("alerts.discord_error", status=resp.status_code, body=resp.text[:200])
            return False
        return True
