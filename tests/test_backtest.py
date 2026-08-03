"""Backtester — including the lookahead-bias guard, which is the whole point."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.backtest import MIN_BARS_BEFORE_TRADING, Backtester
from src.config import Settings
from src.data.market_data import MarketDataProvider
from src.models import Candle
from strategies.base import load_strategies


class FakeMarketData(MarketDataProvider):
    def __init__(self, candles: dict[str, list[Candle]]) -> None:
        self.candles = candles

    def get_candles(self, symbol, timeframe="1Day", limit=250):
        return self.candles.get(symbol, [])[-limit:]


def uptrend(n: int = 300) -> list[Candle]:
    """
    A grind higher in 10-day cycles: five up days, three down, then a
    higher-volume breakout day. Tuned so the trend-breakout rule genuinely
    fires (RSI stays in the 60s-70s and relative volume spikes past 1.2) —
    otherwise the tests below would pass vacuously on zero trades.
    """
    start = datetime(2023, 1, 1, tzinfo=timezone.utc)
    out = []
    price = 100.0
    for i in range(n):
        cycle = i % 10
        if cycle < 5:
            price *= 1.003
            volume = 1_000_000
        elif cycle < 8:
            price *= 0.996
            volume = 700_000
        else:
            price *= 1.008
            volume = 3_000_000
        out.append(
            Candle(
                ts=start + timedelta(days=i),
                open=price * 0.998,
                high=price * 1.008,
                low=price * 0.992,
                close=price,
                volume=volume,
            )
        )
    return out


@pytest.fixture
def settings() -> Settings:
    return Settings(anthropic_api_key="test", min_risk_reward=1.0, _env_file=None)


def test_short_history_is_skipped_not_traded(settings):
    data = FakeMarketData({"AAPL": uptrend(50)})
    result = Backtester(settings, data, load_strategies()).run(["AAPL"])

    assert result.trades == []
    assert "AAPL" in result.skipped


def test_missing_symbol_is_reported(settings):
    result = Backtester(settings, FakeMarketData({}), load_strategies()).run(["NOPE"])
    assert result.trades == []
    assert result.bars_tested == 0


def test_the_fixture_actually_produces_trades(settings):
    """
    Guards every other test in this file: if the synthetic series stops firing
    setups, the assertions below would pass on an empty trade list.
    """
    data = FakeMarketData({"AAPL": uptrend(300)})
    result = Backtester(settings, data, load_strategies()).run(["AAPL"])
    assert result.trades, "fixture generated no trades — the tests below prove nothing"


def test_backtest_runs_and_reports(settings):
    data = FakeMarketData({"AAPL": uptrend(300)})
    result = Backtester(settings, data, load_strategies(), starting_equity=10_000).run(["AAPL"])

    assert result.bars_tested > 0
    assert result.starting_equity == 10_000
    report = result.report()
    assert "BACKTEST RESULT" in report
    assert "curve-fitted" in report  # the warning must survive refactors


def test_entries_fill_on_the_bar_after_the_signal(settings):
    """
    The core lookahead guard: a decision made on bar i must fill at bar i+1's
    open, never at bar i's close.
    """
    candles = uptrend(300)
    data = FakeMarketData({"AAPL": candles})
    result = Backtester(settings, data, load_strategies()).run(["AAPL"])

    assert result.trades
    by_ts = {c.ts: c for c in candles}
    for trade in result.trades:
        assert trade.entry == pytest.approx(by_ts[trade.opened_at].open)
        # The fill bar must be strictly later than the last bar the rules saw.
        assert trade.opened_at > candles[MIN_BARS_BEFORE_TRADING - 1].ts


def test_exits_land_on_the_stop_or_the_target(settings):
    data = FakeMarketData({"AAPL": uptrend(300)})
    result = Backtester(settings, data, load_strategies()).run(["AAPL"])

    for trade in result.trades:
        if trade.exit_reason == "stop":
            assert trade.exit == pytest.approx(trade.stop)
            assert trade.r_multiple == pytest.approx(-1.0, abs=0.01)
        elif trade.exit_reason == "target":
            assert trade.exit == pytest.approx(trade.target)
            assert trade.r_multiple > 0


def test_no_trades_start_before_the_warmup_window(settings):
    candles = uptrend(300)
    data = FakeMarketData({"AAPL": candles})
    result = Backtester(settings, data, load_strategies()).run(["AAPL"])

    earliest_allowed = candles[MIN_BARS_BEFORE_TRADING].ts
    for trade in result.trades:
        assert trade.opened_at >= earliest_allowed


def test_open_positions_are_closed_at_the_end(settings):
    data = FakeMarketData({"AAPL": uptrend(300)})
    result = Backtester(settings, data, load_strategies()).run(["AAPL"])
    assert all(t.status == "closed" for t in result.trades)
