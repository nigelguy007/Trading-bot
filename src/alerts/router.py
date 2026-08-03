"""
Fan-out to every configured alert channel.

An alert failing must never break a trading cycle, so every send is best-effort
and logged. If nothing is configured the router says so once at startup rather
than silently swallowing every signal.
"""
from __future__ import annotations

from src.alerts import formatter
from src.alerts.discord import DiscordAlerts
from src.alerts.telegram import TelegramAlerts
from src.config import Settings
from src.logging_setup import get_logger
from src.models import PositionSizing, RiskDecision, Signal
from src.portfolio import PortfolioState

log = get_logger(__name__)


class AlertRouter:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.discord = DiscordAlerts(settings.discord_webhook_url)
        self.telegram = TelegramAlerts(settings.telegram_bot_token, settings.telegram_chat_id)

        if not self.enabled:
            log.warning(
                "alerts.none_configured",
                note="set DISCORD_WEBHOOK_URL or TELEGRAM_BOT_TOKEN+TELEGRAM_CHAT_ID "
                     "or you will not hear about signals",
            )

    @property
    def enabled(self) -> bool:
        return self.discord.enabled or self.telegram.enabled

    def close(self) -> None:
        self.discord.close()
        self.telegram.close()

    # ------------------------------------------------------------------ #
    def signal(
        self,
        signal: Signal,
        sizing: PositionSizing,
        decision: RiskDecision,
        *,
        paper: bool,
        actionable: bool,
    ) -> None:
        mode = "alert" if actionable else "auto"
        text = formatter.signal_text(signal, sizing, decision, mode=mode, paper=paper)
        self.discord.send(text, embed=formatter.discord_embed(signal, sizing, decision, paper))
        # Buttons only when there is genuinely something to approve.
        self.telegram.send(text, signal_id=signal.id if actionable else None)

    def rejection(self, signal: Signal, decision: RiskDecision) -> None:
        """Blocked setups are logged loudly but only pushed to Discord —
        a phone buzzing for every rejection trains you to ignore it."""
        self.discord.send(formatter.rejection_text(signal, decision))

    def halt(self, decision: RiskDecision, state: PortfolioState) -> None:
        self.broadcast(formatter.halt_text(decision, state))

    def fill(self, signal: Signal, qty: float, order_id: str, paper: bool) -> None:
        self.broadcast(formatter.fill_text(signal, qty, order_id, paper))

    def broadcast(self, text: str) -> None:
        self.discord.send(text)
        self.telegram.send(text)
