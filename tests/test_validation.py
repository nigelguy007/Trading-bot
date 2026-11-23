"""
Costs, date windows, gap fills and out-of-sample validation.

The reel's steps 2 and 3: a backtest that ignores fees and slippage lies, and a
strategy judged on the data it was built on proves nothing.
"""
from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

import pytest

from src.backtest import Backtester, CostModel, check_exit
from src.config import Settings
from src.models import Candle
from src.validation import (
    MIN_TRADES_TO_JUDGE,
    CoinFlipResult,
    coin_flip_benchmark,
    walk_forward,
)
from strategies.base import load_strategies
from tests.test_backtest import FakeMarketData, uptrend

UTC = timezone.utc


@pytest.fixture
def settings() -> Settings:
    return Settings(anthropic_api_key="test", min_risk_reward=1.0, _env_file=None)


def bar(o, h, l, c, day=0) -> Candle:
    return Candle(ts=datetime(2024, 1, 1, tzinfo=UTC) + timedelta(days=day),
                  open=o, high=h, low=l, close=c, volume=1)


# --- CostModel ---------------------------------------------------------- #
def test_slippage_always_hurts():
    costs = CostModel(slippage_bps=10)  # 0.1%
    assert costs.slip(100, "long", opening=True) == pytest.approx(100.1)   # buy higher
    assert costs.slip(100, "long", opening=False) == pytest.approx(99.9)   # sell lower
    assert costs.slip(100, "short", opening=True) == pytest.approx(99.9)   # sell lower
    assert costs.slip(100, "short", opening=False) == pytest.approx(100.1)  # buy back higher


def test_commission_per_unit_and_percent():
    costs = CostModel(commission_per_unit=2.25, commission_pct=0.1)
    assert costs.commission(2, 1000) == pytest.approx(2 * 2.25 + 2000 * 0.001)


def test_negative_costs_are_rejected():
    with pytest.raises(ValueError):
        CostModel(slippage_bps=-1)


def test_free_model_says_so():
    assert CostModel().is_free
    assert "upper bound" in CostModel().describe()


# --- exits ---------------------------------------------------------------- #
def test_a_gap_through_the_stop_fills_at_the_open_not_the_stop():
    price, reason = check_exit("long", stop=95, target=110, bar=bar(90, 92, 88, 91), costs=CostModel())
    assert price == 90
    assert reason == "stop (gap)"


def test_a_short_gap_through_the_stop_fills_at_the_open():
    price, reason = check_exit("short", stop=105, target=90, bar=bar(108, 109, 104, 107), costs=CostModel())
    assert price == 108
    assert reason == "stop (gap)"


def test_touching_both_levels_counts_as_the_stop():
    price, reason = check_exit("long", stop=95, target=110, bar=bar(100, 111, 94, 105), costs=CostModel())
    assert reason == "stop"
    assert price == 95


def test_the_target_limit_is_not_slipped_but_the_stop_is():
    costs = CostModel(slippage_bps=100)  # 1%
    tp, _ = check_exit("long", stop=95, target=110, bar=bar(100, 111, 99, 105), costs=costs)
    sl, _ = check_exit("long", stop=95, target=110, bar=bar(100, 101, 94, 96), costs=costs)
    assert tp == 110
    assert sl == pytest.approx(95 * 0.99)


# --- the backtester with costs and windows ------------------------------ #
def test_costs_reduce_pnl_by_exactly_what_was_charged(settings):
    data = FakeMarketData({"AAPL": uptrend(400)})
    free = Backtester(settings, data, load_strategies()).run(["AAPL"], bars=400)
    costly = Backtester(
        settings, data, load_strategies(), costs=CostModel(commission_per_unit=0.5)
    ).run(["AAPL"], bars=400)

    assert free.trades, "fixture must trade or this test proves nothing"
    assert len(costly.trades) == len(free.trades)  # commission alone does not move fills
    assert costly.total_costs > 0
    # Sizes compound on equity, so compare each trade with itself: net P&L is
    # gross P&L minus exactly the commission charged on both sides.
    for t in costly.trades:
        sign = 1 if t.direction == "long" else -1
        gross = (t.exit - t.entry) * t.qty * sign
        assert t.costs == pytest.approx(2 * 0.5 * t.qty)
        assert t.pnl == pytest.approx(gross - t.costs)
    assert costly.ending_equity < free.ending_equity


def test_slippage_makes_every_entry_worse(settings):
    data = FakeMarketData({"AAPL": uptrend(400)})
    free = Backtester(settings, data, load_strategies()).run(["AAPL"], bars=400)
    slipped = Backtester(
        settings, data, load_strategies(), costs=CostModel(slippage_bps=5)
    ).run(["AAPL"], bars=400)

    pairs = list(zip(free.trades, slipped.trades))
    assert pairs
    for f, s in pairs:
        if f.direction == "long":
            assert s.entry > f.entry
        else:
            assert s.entry < f.entry


def test_a_window_only_opens_trades_inside_it(settings):
    candles = uptrend(600)
    data = FakeMarketData({"AAPL": candles})
    start, end = candles[300].ts, candles[450].ts

    result = Backtester(settings, data, load_strategies()).run(["AAPL"], start=start, end=end)

    assert result.trades
    for t in result.trades:
        assert start <= t.opened_at < end
        assert t.closed_at < end  # end is exclusive; nothing leaks across


def test_warm_up_bars_before_the_window_are_allowed(settings):
    """Indicators may use bars before `start` — they are the past, not the future."""
    candles = uptrend(400)
    data = FakeMarketData({"AAPL": candles})
    result = Backtester(settings, data, load_strategies()).run(["AAPL"], start=candles[210].ts)
    assert "AAPL" not in result.skipped


def test_a_window_without_enough_warm_up_is_skipped(settings):
    candles = uptrend(260)
    data = FakeMarketData({"AAPL": candles})
    result = Backtester(settings, data, load_strategies()).run(
        ["AAPL"], start=candles[0].ts, end=candles[150].ts
    )
    assert "AAPL" in result.skipped


# --- coin flip ------------------------------------------------------------ #
def random_walk(n: int, seed: int = 7) -> list[Candle]:
    rng = random.Random(seed)
    start = datetime(2010, 1, 1, tzinfo=UTC)
    out, price = [], 100.0
    for i in range(n):
        o = price
        price *= 1 + rng.gauss(0, 0.01)
        out.append(Candle(ts=start + timedelta(days=i), open=o,
                          high=max(o, price) * 1.004, low=min(o, price) * 0.996,
                          close=price, volume=1_000_000))
    return out


def test_p_value_is_never_zero_and_bounded():
    r = CoinFlipResult(strategy_total_r=100, random_total_r=[0.0] * 99)
    assert r.p_value == pytest.approx(1 / 100)
    assert CoinFlipResult(strategy_total_r=0).p_value == 1.0


def test_random_trades_on_a_random_walk_do_not_beat_a_coin_flip():
    """Random entries ARE a coin flip; they must not look like an edge."""
    from src.backtest import BacktestTrade

    candles = random_walk(800)
    data = FakeMarketData({"X": candles})
    rng = random.Random(1)
    fake_trades = []
    for _ in range(60):
        i = rng.randrange(0, 700)
        e = candles[i].close
        fake_trades.append(BacktestTrade(
            symbol="X", strategy="t", direction="long", opened_at=candles[i].ts,
            closed_at=candles[i + 5].ts, qty=1, entry=e, exit=e, stop=e * 0.98,
            target=e * 1.04, pnl=0, r_multiple=rng.choice([-1.0, 2.0]) - 0.3, exit_reason="t",
        ))
    result = coin_flip_benchmark(data, fake_trades, CostModel(), None, None, simulations=300)

    assert result.trades_matched == 60
    assert not result.beats_coin_flip


def test_the_benchmark_is_reproducible():
    from src.backtest import BacktestTrade

    candles = random_walk(300)
    data = FakeMarketData({"X": candles})
    t = BacktestTrade(symbol="X", strategy="t", direction="long", opened_at=candles[10].ts,
                      closed_at=candles[20].ts, qty=1, entry=100, exit=101, stop=98,
                      target=104, pnl=1, r_multiple=0.5, exit_reason="t")
    a = coin_flip_benchmark(data, [t] * 5, CostModel(), None, None, simulations=50, seed=3)
    b = coin_flip_benchmark(data, [t] * 5, CostModel(), None, None, simulations=50, seed=3)
    assert a.random_total_r == b.random_total_r


# --- walk-forward --------------------------------------------------------- #
def test_walk_forward_splits_without_overlap(settings):
    candles = uptrend(900)
    data = FakeMarketData({"AAPL": candles})
    bt = Backtester(settings, data, load_strategies(), costs=CostModel(slippage_bps=2))
    start, split = candles[250].ts, candles[550].ts

    report = walk_forward(bt, ["AAPL"], start, split, simulations=100)

    assert all(t.opened_at < split and t.closed_at < split for t in report.in_sample.trades)
    assert all(t.opened_at >= split for t in report.out_of_sample.trades)
    text = report.report()
    assert "OUT-OF-SAMPLE" in text and "COIN-FLIP" in text and "SPENT" in text


def test_too_few_trades_is_inconclusive_not_a_pass(settings):
    candles = uptrend(500)
    data = FakeMarketData({"AAPL": candles})
    bt = Backtester(settings, data, load_strategies())
    report = walk_forward(bt, ["AAPL"], candles[210].ts, candles[400].ts, simulations=50)

    assert report.out_of_sample.metrics.total_trades < MIN_TRADES_TO_JUDGE
    assert report.verdict.startswith("INCONCLUSIVE")


def test_bad_split_order_is_rejected(settings):
    bt = Backtester(settings, FakeMarketData({}), load_strategies())
    t0 = datetime(2020, 1, 1, tzinfo=UTC)
    with pytest.raises(ValueError):
        walk_forward(bt, ["X"], t0, t0 - timedelta(days=1))
