"""
Telegram alerts, with Approve / Deny buttons.

The buttons are the "approve trades from your phone" upgrade: an actionable
signal arrives with an inline keyboard, and tapping it fires a callback the
webhook server turns into an approval. Anything not actionable goes out as
plain text.
"""
from __future__ import annotations

import httpx

from src.logging_setup import get_logger

log = get_logger(__name__)

TELEGRAM_LIMIT = 4096


class TelegramAlerts:
    name = "telegram"

    def __init__(self, bot_token: str, chat_id: str) -> None:
        self.bot_token = bot_token
        self.chat_id = chat_id
        self._client = httpx.Client(timeout=15.0)

    @property
    def enabled(self) -> bool:
        return bool(self.bot_token and self.chat_id)

    @property
    def _base(self) -> str:
        return f"https://api.telegram.org/bot{self.bot_token}"

    def close(self) -> None:
        self._client.close()

    def send(self, text: str, signal_id: str | None = None) -> bool:
        if not self.enabled:
            return False

        payload: dict = {
            "chat_id": self.chat_id,
            "text": f"```\n{text[: TELEGRAM_LIMIT - 20]}\n```",
            "parse_mode": "Markdown",
        }
        if signal_id:
            payload["reply_markup"] = {
                "inline_keyboard": [
                    [
                        {"text": "✅ Approve", "callback_data": f"approve:{signal_id}"},
                        {"text": "❌ Deny", "callback_data": f"deny:{signal_id}"},
                    ]
                ]
            }

        return self._post("sendMessage", payload)

    def answer_callback(self, callback_query_id: str, text: str) -> bool:
        return self._post(
            "answerCallbackQuery",
            {"callback_query_id": callback_query_id, "text": text[:200]},
        )

    def _post(self, method: str, payload: dict) -> bool:
        try:
            resp = self._client.post(f"{self._base}/{method}", json=payload)
        except httpx.HTTPError as exc:
            log.warning("alerts.telegram_network_error", method=method, error=str(exc))
            return False

        if resp.status_code == 401:
            log.error("alerts.telegram_unauthorized", hint="check TELEGRAM_BOT_TOKEN")
            return False
        if resp.status_code >= 400:
            log.warning("alerts.telegram_error", status=resp.status_code, body=resp.text[:200])
            return False
        return True

    def set_webhook(self, url: str) -> bool:
        """Point Telegram at the bot's own webhook endpoint so buttons work."""
        return self._post("setWebhook", {"url": url, "allowed_updates": ["callback_query"]})
