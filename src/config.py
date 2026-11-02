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


def is_placeholder(value: str) -> bool:
    """
    True when a setting still holds the text .env.example shipped with.

    Worth a shared helper: a placeholder is non-empty, so every naive
    "is it set?" check passes and the user gets a confusing 401 instead of
    the graceful offline fallback they should have had.
    """
    if not value:
        return False
    v = value.strip().lower()
    return (
        v.startswith("your_")
        or v.endswith("_here")
        or v in {"changeme", "change_me", "xxx", "todo"}
    )


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

    # --- Broker: Trading 212 (Invest / Stocks ISA, UK) --------------------
    # Real shares, no leverage, and inside an ISA the gains are untaxed — but
    # the API has NO bracket orders. See docs/trading-212.md.
    t212_api_key: str = ""
    t212_api_secret: str = ""
    t212_demo: bool = True
    # Pin symbols to T212 tickers: "AAPL=AAPL_US_EQ,VOD=VODl_EQ".
    # Anything unmapped is resolved from /metadata/instruments.
    t212_tickers: str = ""
    # Seconds to wait for a market entry to fill before giving up on placing
    # its stop. Every second here is a second the position is unprotected.
    t212_fill_timeout: float = Field(default=20.0, gt=0, le=300)

    # --- Broker: IG (IG Index / IG Markets, UK) ---------------------------
    # IG is a spread betting / CFD provider: positions are LEVERAGED and sized
    # in currency-per-point, not in shares. See docs/ig-spread-betting.md.
    broker: Literal["auto", "alpaca", "ig", "t212", "simulated"] = "auto"
    # A second broker to run alongside the first. Both accounts are then
    # treated as ONE portfolio by the risk gate — see docs/multi-broker.md.
    # Without this, two brokers means two independent sets of limits, and a
    # -3% day at each is a -6% day nothing stopped.
    secondary_broker: Literal["", "alpaca", "ig", "t212", "simulated"] = ""
    # Which venue handles which symbol: "FTSE=ig,AAPL=t212". Anything unlisted
    # goes to the primary broker (or to whichever one supports its asset class).
    broker_routes: str = ""
    ig_api_key: str = ""
    ig_username: str = ""
    ig_password: str = ""
    ig_account_id: str = ""
    ig_demo: bool = True
    # IG rejects bursts; its published guidance is a handful of requests per
    # few seconds. Keep a floor between trading calls.
    ig_min_request_interval: float = 1.0
    # Account currency, for display only. IG reports it per account.
    ig_currency: str = "GBP"
    # Spread bets are geared, so exposure legitimately exceeds equity and the
    # equity-based MAX_POSITION_PCT cap would crush every size. This is the
    # gearing ceiling instead: 300% = at most 3x account equity at risk of
    # moving against you, before the stop.
    ig_max_exposure_pct: float = Field(default=300.0, gt=0, le=2000)
    # Rough deposit factor used to sanity-check a size against available margin
    # before IG is asked. IG's real requirement varies by market (often 5-20%
    # on indices and major FX, far higher on small caps), and IG itself is the
    # authority — this only catches the obviously-unfundable case early.
    ig_margin_pct: float = Field(default=20.0, gt=0, le=100)
    # Smallest stake IG will accept, in currency per point. Market-dependent;
    # 0.50 is the usual floor on its main spread betting markets.
    ig_min_deal_size: float = Field(default=0.5, gt=0)
    # Pin watchlist symbols to IG epics: "AAPL=UA.D.AAPL.CASH.IP,FTSE=IX.D.FTSE.DAILY.IP".
    # Anything unmapped falls back to IG's own market search, which is a guess.
    ig_epics: str = ""

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
        The stop distance below which the size cap starts overriding the
        per-trade risk budget.

        Notional needed for a full-risk position is risk% / stop-distance%, so a
        1% risk budget against a 2% stop wants a position worth 50% of equity.
        If the position cap is tighter than that, size gets trimmed and the
        trade quietly risks less than the configured 1%. That is safe, but it is
        worth knowing about rather than discovering in the sizing output.

        On a spread betting account the governing cap is the gearing ceiling
        rather than MAX_POSITION_PCT, so the same arithmetic uses that instead.
        """
        cap = self.ig_max_exposure_pct if self.spread_betting else self.max_position_pct
        return self.risk_per_trade_pct / cap * 100.0

    def live_enabled_for(self, broker: str) -> bool:
        """
        Whether the config authorises REAL orders through a named broker.

        Three independent switches must agree, and the third one is
        broker-specific: Alpaca has ALPACA_PAPER, IG has IG_DEMO. Keeping them
        separate means a leftover `ALPACA_PAPER=false` cannot quietly authorise
        live spread bets, and vice versa.
        """
        if self.trading_mode != "live" or self.confirm_live != LIVE_CONFIRMATION_PHRASE:
            return False
        if broker == "ig":
            return not self.ig_demo
        if broker == "t212":
            return not self.t212_demo
        return not self.alpaca_paper

    @property
    def live_trading_enabled(self) -> bool:
        """The live gate for whichever broker is actually in use."""
        broker = self.active_broker
        return self.live_enabled_for("alpaca" if broker == "simulated" else broker)

    @property
    def broker_base_url(self) -> str:
        """Alpaca's host. Asks Alpaca's own gate, not whichever broker is active."""
        return (
            "https://api.alpaca.markets"
            if self.live_enabled_for("alpaca")
            else "https://paper-api.alpaca.markets"
        )

    @property
    def has_broker_keys(self) -> bool:
        """Template placeholders count as absent, so an unedited .env falls
        back to the local simulator instead of 401-ing against Alpaca."""
        return (
            bool(self.alpaca_api_key and self.alpaca_secret_key)
            and not is_placeholder(self.alpaca_api_key)
            and not is_placeholder(self.alpaca_secret_key)
        )

    @property
    def has_t212_keys(self) -> bool:
        return bool(self.t212_api_key) and not is_placeholder(self.t212_api_key)

    @property
    def t212_base_url(self) -> str:
        """Demo and live are different hosts. Demo is the default on purpose."""
        return (
            "https://demo.trading212.com"
            if self.t212_demo
            else "https://live.trading212.com"
        )

    @property
    def t212_ticker_map(self) -> dict[str, str]:
        """Parsed T212_TICKERS. Malformed entries are skipped, not fatal."""
        mapping: dict[str, str] = {}
        for pair in self.t212_tickers.split(","):
            symbol, sep, ticker = pair.partition("=")
            if sep and symbol.strip() and ticker.strip():
                mapping[symbol.strip().upper()] = ticker.strip()
        return mapping

    @property
    def route_map(self) -> dict[str, str]:
        """Parsed BROKER_ROUTES: symbol -> broker name."""
        mapping: dict[str, str] = {}
        for pair in self.broker_routes.split(","):
            symbol, sep, broker = pair.partition("=")
            if sep and symbol.strip() and broker.strip():
                mapping[symbol.strip().upper()] = broker.strip().lower()
        return mapping

    @property
    def has_ig_keys(self) -> bool:
        return all(
            bool(v) and not is_placeholder(v)
            for v in (self.ig_api_key, self.ig_username, self.ig_password)
        )

    @property
    def ig_base_url(self) -> str:
        """Demo and live are different hosts. Demo is the default on purpose."""
        return (
            "https://demo-api.ig.com/gateway/deal"
            if self.ig_demo
            else "https://api.ig.com/gateway/deal"
        )

    @property
    def active_broker(self) -> str:
        """Which adapter build_broker should construct as the primary."""
        if self.broker != "auto":
            return self.broker
        if self.has_ig_keys:
            return "ig"
        if self.has_t212_keys:
            return "t212"
        if self.has_broker_keys:
            return "alpaca"
        return "simulated"

    @property
    def broker_names(self) -> list[str]:
        """Every venue this run will trade through, primary first."""
        names = [self.active_broker]
        if self.secondary_broker and self.secondary_broker not in names:
            names.append(self.secondary_broker)
        return names

    @property
    def multi_broker(self) -> bool:
        return len(self.broker_names) > 1

    @property
    def ig_epic_map(self) -> dict[str, str]:
        """Parsed IG_EPICS. Malformed entries are skipped, not fatal."""
        mapping: dict[str, str] = {}
        for pair in self.ig_epics.split(","):
            symbol, sep, epic = pair.partition("=")
            if sep and symbol.strip() and epic.strip():
                mapping[symbol.strip().upper()] = epic.strip()
        return mapping

    def is_spread_bet(self, broker: str) -> bool:
        """
        True when THAT broker's sizes are a stake per point, not a share count.

        Per-broker rather than global because the two can run side by side: the
        same scan can route one signal to IG as a £2.50/point spread bet and
        another to Trading 212 as 40 shares. A single global flag would size
        one of them in the wrong unit.
        """
        return broker == "ig"

    def size_unit_for(self, broker: str) -> str:
        return f"{self.ig_currency}/point" if self.is_spread_bet(broker) else "shares"

    @property
    def spread_betting(self) -> bool:
        """Whether the PRIMARY broker is a spread betting account."""
        return self.is_spread_bet(self.active_broker)

    @property
    def size_unit(self) -> str:
        return self.size_unit_for(self.active_broker)

    @property
    def has_llm_key(self) -> bool:
        if self.llm_provider == "openrouter":
            return bool(self.openrouter_api_key) and not is_placeholder(self.openrouter_api_key)
        return bool(self.anthropic_api_key) and not is_placeholder(self.anthropic_api_key)

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
        if self.spread_betting:
            if self.ig_demo:
                blockers.append("IG_DEMO is true")
        elif self.alpaca_paper:
            blockers.append("ALPACA_PAPER is true")
        if self.confirm_live != LIVE_CONFIRMATION_PHRASE:
            blockers.append(f"CONFIRM_LIVE is not '{LIVE_CONFIRMATION_PHRASE}'")
        return blockers


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
