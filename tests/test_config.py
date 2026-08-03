"""Settings — especially the switches that guard real money."""
from __future__ import annotations

import pytest

from src.config import LIVE_CONFIRMATION_PHRASE, Settings


def make(**kw) -> Settings:
    kw.setdefault("anthropic_api_key", "test")
    return Settings(_env_file=None, **kw)


def test_defaults_are_safe():
    s = make()
    assert s.trading_mode == "paper"
    assert s.alpaca_paper is True
    assert s.execution_mode == "alert"
    assert not s.live_trading_enabled


def test_trailing_whitespace_is_stripped():
    """A pasted key with a trailing space is the classic phantom 401."""
    assert make(anthropic_api_key="  sk-ant-secret  ").anthropic_api_key == "sk-ant-secret"


def test_live_needs_all_three_switches():
    assert not make(trading_mode="live").live_trading_enabled
    assert not make(trading_mode="live", alpaca_paper=False).live_trading_enabled
    assert not make(
        trading_mode="live", alpaca_paper=False, confirm_live="yes"
    ).live_trading_enabled

    assert make(
        trading_mode="live",
        alpaca_paper=False,
        confirm_live=LIVE_CONFIRMATION_PHRASE,
    ).live_trading_enabled


def test_blockers_explain_why_live_is_off():
    blockers = make().live_mode_blockers()
    assert len(blockers) == 3
    assert any("TRADING_MODE" in b for b in blockers)


def test_paper_url_unless_fully_live():
    assert "paper-api" in make().broker_base_url
    assert "paper-api" not in make(
        trading_mode="live", alpaca_paper=False, confirm_live=LIVE_CONFIRMATION_PHRASE
    ).broker_base_url


def test_watchlist_parsing():
    s = make(watchlist=" aapl , msft,, nvda ", crypto_watchlist="btc/usd")
    assert s.symbols == ["AAPL", "MSFT", "NVDA"]
    assert s.crypto_symbols == ["BTC/USD"]
    assert s.all_symbols == ["AAPL", "MSFT", "NVDA", "BTC/USD"]


def test_per_trade_risk_cannot_exceed_the_daily_limit():
    with pytest.raises(ValueError, match="cannot exceed"):
        make(risk_per_trade_pct=5.0, daily_loss_limit_pct=3.0)


def test_portfolio_risk_must_allow_at_least_one_trade():
    with pytest.raises(ValueError, match="at least"):
        make(risk_per_trade_pct=2.0, max_portfolio_risk_pct=1.0)


def test_risk_per_trade_is_bounded():
    with pytest.raises(ValueError):
        make(risk_per_trade_pct=50.0)


def test_report_time_parsing():
    assert make(daily_report_time="16:15").report_time.hour == 16
    with pytest.raises(ValueError, match="HH:MM"):
        make(daily_report_time="25:99")
