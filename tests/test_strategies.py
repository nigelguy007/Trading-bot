"""Strategy rules — they must fire only when every condition holds."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.models import TechnicalSnapshot
from strategies import MeanReversion, TrendBreakout, load_strategies


def snapshot(**kw) -> TechnicalSnapshot:
    defaults = dict(
        symbol="AAPL",
        as_of=datetime(2024, 6, 1, tzinfo=timezone.utc),
        last_price=110.0,
        prev_close=108.0,
        change_pct=1.85,
        sma20=105.0, sma50=100.0, sma200=95.0,
        ema9=109.0, ema21=106.0,
        rsi14=62.0, atr14=2.0, atr_pct=1.8,
        macd=1.2, macd_signal=0.9, macd_hist=0.3,
        bb_upper=112.0, bb_lower=98.0, bb_pct=85.0,
        volume=2_000_000, avg_volume20=1_000_000, rel_volume=2.0,
        trend="strong uptrend",
        support_levels=[105.0], resistance_levels=[115.0],
        range_high_20=110.0, range_low_20=100.0,
    )
    defaults.update(kw)
    return TechnicalSnapshot(**defaults)


# --- trend breakout ----------------------------------------------------- #
def test_breakout_fires_on_a_clean_setup():
    setup = TrendBreakout().evaluate(snapshot())

    assert setup is not None
    assert setup.direction == "long"
    assert setup.stop < setup.entry < setup.target
    assert setup.stop == pytest.approx(110.0 - 1.5 * 2.0)


def test_breakout_needs_volume():
    assert TrendBreakout().evaluate(snapshot(rel_volume=0.7)) is None


def test_breakout_needs_price_at_the_range_high():
    assert TrendBreakout().evaluate(snapshot(range_high_20=130.0)) is None


def test_breakout_skips_overbought_momentum():
    assert TrendBreakout().evaluate(snapshot(rsi14=85.0)) is None


def test_breakout_needs_an_uptrend():
    assert TrendBreakout().evaluate(snapshot(sma20=115.0, sma50=120.0)) is None


def test_breakout_without_history_does_not_fire():
    assert TrendBreakout().evaluate(snapshot(sma20=0.0, sma50=0.0)) is None


# --- mean reversion ----------------------------------------------------- #
def oversold(**kw) -> TechnicalSnapshot:
    defaults = dict(
        last_price=96.0, rsi14=25.0, bb_lower=97.0, sma20=104.0,
        sma50=100.0, sma200=90.0, trend="uptrend",
    )
    defaults.update(kw)
    return snapshot(**defaults)


def test_mean_reversion_fires_on_a_dip_in_an_uptrend():
    setup = MeanReversion().evaluate(oversold())

    assert setup is not None
    assert setup.direction == "long"
    assert setup.target == pytest.approx(104.0)  # the 20 SMA
    assert setup.stop < setup.entry


def test_mean_reversion_refuses_to_catch_a_falling_knife():
    """Below the 200 SMA this is a downtrend, not a pullback."""
    assert MeanReversion().evaluate(oversold(sma200=120.0)) is None


def test_mean_reversion_needs_oversold():
    assert MeanReversion().evaluate(oversold(rsi14=55.0)) is None


def test_mean_reversion_needs_the_band_tag():
    assert MeanReversion().evaluate(oversold(bb_lower=90.0)) is None


def test_mean_reversion_skips_broken_structure():
    # 20% below the 50 SMA is a regime change, not a dip.
    assert MeanReversion().evaluate(oversold(sma50=130.0)) is None


def test_mean_reversion_needs_a_mean_above_price():
    assert MeanReversion().evaluate(oversold(sma20=95.0)) is None


# --- loader ------------------------------------------------------------- #
def test_load_all_strategies():
    assert len(load_strategies()) == 2


def test_load_named_strategy():
    loaded = load_strategies(["trend_breakout"])
    assert len(loaded) == 1 and loaded[0].name == "trend_breakout"


def test_unknown_strategy_is_an_error():
    with pytest.raises(ValueError, match="unknown strategy"):
        load_strategies(["does_not_exist"])


def test_strategies_never_raise_on_degenerate_input():
    """A bad snapshot must return None, not take the scan down."""
    empty = snapshot(
        last_price=0.0, atr14=0.0, sma20=0.0, sma50=0.0, sma200=0.0,
        bb_lower=0.0, range_high_20=0.0,
    )
    for strategy in load_strategies():
        assert strategy.evaluate(empty) is None
