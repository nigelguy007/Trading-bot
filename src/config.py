"""
Typed settings loaded from .env.

Two things this module is deliberately strict about, because both are ways
people lose money:

1. Trailing whitespace on API keys. Pasting a key out of a browser very often
   drags a space or newline along, and the resulting 401 looks like a bad key.
   Every string field is stripped on the way in.
2. Going live. `live_trading_enabled` requires three independent switches to
   agree. Flipping one by accident does nothing.
"""
from __future__ import annotations

from datetime import time as dt_time
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parent.parent

# The exact phrase that must appear in CONFIRM_LIVE before real orders are possible.
LIVE_CONFIRMATION_PHRASE = "I_UNDERSTAND_THE_RISK"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=REPO_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- Claude / LLM -----------------------------------------------------
    llm_provider: Literal["anthropic", "openrouter"] = "anthropic"
    anthropic_api_key: str = ""
    claude_model: str = "claude-fable-5"
    claude_effort: Literal["low", "medium", "high", "xhigh", "max"] = "high"
    claude_max_tokens: int = 16_000
    openrouter_api_key: str = ""
    openrouter_model: str = "anthropic/claude-sonnet-4.5"

    # --- Broker -----------------------------------------------------------
    alpaca_api_key: str = ""
    alpaca_secret_key: str = ""
    alpaca_paper: bool = True
    alpaca_data_feed: Literal["iex", "sip"] = "iex"

    # --- News -------------------------------------------------------------
    news_api_key: str = ""
    news_cache_ttl: int = 900

    # --- Alerts -----------------------------------------------------------
    discord_webhook_url: str = ""
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    # --- Webhook server ---------------------------------------------------
    tradingview_webhook_secret: str = ""
    webhook_host: str = "0.0.0.0"
    webhook_port: int = 8001

    # --- Universe ---------------------------------------------------------
    watchlist: str = "SPY,QQQ,AAPL,MSFT,NVDA"
    crypto_watchlist: str = ""

    # --- Mode -------------------------------------------------------------
    trading_mode: Literal["paper", "live"] = "paper"
    confirm_live: str = ""
    execution_mode: Literal["alert", "auto"] = "alert"
    approval_ttl_minutes: int = 45

    # --- Risk (guide §8) --------------------------------------------------
    risk_per_trade_pct: float = Field(default=1.0, gt=0, le=5)
    daily_loss_limit_pct: float = Field(default=3.0, gt=0, le=50)
    max_drawdown_pct: float = Field(default=15.0, gt=0, le=90)
    max_open_positions: int = Field(default=5, ge=1)
    max_correlated_positions: int = Field(default=2, ge=1)
    max_portfolio_risk_pct: float = Field(default=6.0, gt=0, le=50)
    min_risk_reward: float = Field(default=1.5, gt=0)
    max_position_pct: float = Field(default=50.0, gt=0, le=100)
    min_confidence: float = Field(default=0.6, ge=0, le=1)

    # --- Scheduling -------------------------------------------------------
    scan_interval_minutes: int = Field(default=15, ge=1)
    daily_report_time: str = "16:15"
    market_hours_only: bool = True

    # --- Paths / logging --------------------------------------------------
    log_level: str = "INFO"
    log_dir: str = "logs"
    data_dir: str = "data"

    # ------------------------------------------------------------------ #
    # Validators
    # ------------------------------------------------------------------ #
    @field_validator("*", mode="before")
    @classmethod
    def _strip_strings(cls, v):
        """Trailing spaces on a pasted key are the #1 cause of phantom 401s."""
        return v.strip() if isinstance(v, str) else v

    @field_validator("daily_report_time")
    @classmethod
    def _valid_clock(cls, v: str) -> str:
        hh, _, mm = v.partition(":")
        if not (hh.isdigit() and mm.isdigit() and 0 <= int(hh) < 24 and 0 <= int(mm) < 60):
            raise ValueError(f"DAILY_REPORT_TIME must be HH:MM in 24h form, got {v!r}")
        return v

    @model_validator(mode="after")
    def _risk_sanity(self) -> "Settings":
        if self.risk_per_trade_pct > self.daily_loss_limit_pct:
            raise ValueError(
                "RISK_PER_TRADE_PCT cannot exceed DAILY_LOSS_LIMIT_PCT — a single "
                "loss would immediately breach the daily limit."
            )
        if self.max_portfolio_risk_pct < self.risk_per_trade_pct:
            raise ValueError(
                "MAX_PORTFOLIO_RISK_PCT must be at least RISK_PER_TRADE_PCT, "
                "otherwise no trade can ever be opened."
            )
        return self

    # ------------------------------------------------------------------ #
    # Derived helpers
    # ------------------------------------------------------------------ #
    @property
    def symbols(self) -> list[str]:
        return [s.strip().upper() for s in self.watchlist.split(",") if s.strip()]

    @property
    def crypto_symbols(self) -> list[str]:
        return [s.strip().upper() for s in self.crypto_watchlist.split(",") if s.strip()]

    @property
    def all_symbols(self) -> list[str]:
        return self.symbols + self.crypto_symbols

    @property
    def report_time(self) -> dt_time:
        hh, mm = self.daily_report_time.split(":")
        return dt_time(int(hh), int(mm))

    @property
    def implied_min_stop_pct(self) -> float:
        """
        The stop distance below which MAX_POSITION_PCT starts overriding the
        per-trade risk budget.

        Notional needed for a full-risk position is risk% / stop-distance%, so a
        1% risk budget against a 2% stop wants a position worth 50% of equity.
        If the position cap is tighter than that, size gets trimmed and the
        trade quietly risks less than the configured 1%. That is safe, but it is
        worth knowing about rather than discovering in the sizing output.
        """
        return self.risk_per_trade_pct / self.max_position_pct * 100.0

    @property
    def live_trading_enabled(self) -> bool:
        """Three independent switches must agree before a real order is possible."""
        return (
            self.trading_mode == "live"
            and not self.alpaca_paper
            and self.confirm_live == LIVE_CONFIRMATION_PHRASE
        )

    @property
    def broker_base_url(self) -> str:
        return (
            "https://api.alpaca.markets"
            if self.live_trading_enabled
            else "https://paper-api.alpaca.markets"
        )

    @property
    def has_broker_keys(self) -> bool:
        return bool(self.alpaca_api_key and self.alpaca_secret_key)

    @property
    def log_path(self) -> Path:
        p = REPO_ROOT / self.log_dir
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def data_path(self) -> Path:
        p = REPO_ROOT / self.data_dir
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def journal_db(self) -> Path:
        return self.data_path / "journal.db"

    def live_mode_blockers(self) -> list[str]:
        """Why live trading is (correctly) still off. Used by `doctor` and the CLI."""
        blockers = []
        if self.trading_mode != "live":
            blockers.append("TRADING_MODE is not 'live'")
        if self.alpaca_paper:
            blockers.append("ALPACA_PAPER is true")
        if self.confirm_live != LIVE_CONFIRMATION_PHRASE:
            blockers.append(f"CONFIRM_LIVE is not '{LIVE_CONFIRMATION_PHRASE}'")
        return blockers


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
