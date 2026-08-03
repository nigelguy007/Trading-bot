"""Indicator maths, checked against values that can be verified by hand."""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from src.data.indicators import (
    atr,
    bollinger,
    build_snapshot,
    candles_to_frame,
    classify_trend,
    ema,
    rsi,
    sma,
    swing_levels,
)
from src.models import Candle


def series(values: list[float]) -> pd.Series:
    return pd.Series(values, dtype="float64")


def make_candles(closes: list[float], start: datetime | None = None) -> list[Candle]:
    start = start or datetime(2024, 1, 1, tzinfo=timezone.utc)
    return [
        Candle(
            ts=start + timedelta(days=i),
            open=c,
            high=c * 1.01,
            low=c * 0.99,
            close=c,
            volume=1_000_000,
        )
        for i, c in enumerate(closes)
    ]


def test_sma_is_the_mean_of_the_window():
    assert sma(series([1, 2, 3, 4, 5]), 5).iloc[-1] == pytest.approx(3.0)


def test_sma_is_nan_before_the_window_fills():
    assert math.isnan(sma(series([1, 2, 3]), 5).iloc[-1])


def test_ema_weights_recent_values_more():
    values = series(list(range(1, 21)))
    assert ema(values, 10).iloc[-1] > sma(values, 10).iloc[-1]


def test_rsi_of_an_unbroken_rally_is_100():
    assert rsi(series([float(i) for i in range(1, 40)]), 14).iloc[-1] == pytest.approx(100.0)


def test_rsi_of_an_unbroken_selloff_is_0():
    assert rsi(series([float(i) for i in range(40, 1, -1)]), 14).iloc[-1] == pytest.approx(0.0, abs=1e-6)


def test_rsi_stays_in_range_on_noisy_data():
    noisy = series([100 + (i % 7) - 3 for i in range(60)])
    values = rsi(noisy, 14).dropna()
    assert not values.empty
    assert values.between(0, 100).all()


def test_atr_is_positive_and_tracks_range():
    df = candles_to_frame(make_candles([100.0] * 40))
    value = atr(df, 14).iloc[-1]
    assert value > 0
    # Each bar's range is 2% of 100, so ATR should sit near 2.0.
    assert value == pytest.approx(2.0, rel=0.1)


def test_bollinger_bands_bracket_the_mean():
    values = series([100 + (i % 5) for i in range(40)])
    upper, mid, lower = bollinger(values, 20)
    assert lower.iloc[-1] < mid.iloc[-1] < upper.iloc[-1]


def test_classify_trend():
    assert classify_trend(110, 108, 105, 100) == "strong uptrend"
    assert classify_trend(90, 92, 95, 100) == "strong downtrend"
    # Above both the 50 and 200, but the averages aren't stacked.
    assert classify_trend(100, 101, 95, 90) == "uptrend"
    # Above the 200 but below the 50 is genuinely mixed, not an uptrend.
    assert classify_trend(100, 99, 101, 98) == "range / mixed"
    # Missing long averages must not crash — they degrade to what's available.
    assert classify_trend(100, 95, float("nan"), float("nan")) == "uptrend"


def test_swing_levels_split_around_price():
    closes = [100, 105, 102, 108, 103, 99, 104, 110, 106, 101] * 8
    support, resistance = swing_levels(candles_to_frame(make_candles(closes)))
    last = closes[-1]
    assert all(s < last * 1.01 for s in support)
    assert all(r > last * 0.99 for r in resistance)


def test_snapshot_needs_enough_history():
    assert build_snapshot("AAPL", make_candles([100.0] * 10)) is None


def test_snapshot_fields_are_populated():
    closes = [100 + i * 0.5 for i in range(120)]
    snapshot = build_snapshot("AAPL", make_candles(closes))

    assert snapshot is not None
    assert snapshot.symbol == "AAPL"
    assert snapshot.last_price == pytest.approx(closes[-1])
    assert snapshot.atr14 > 0
    assert 0 <= snapshot.rsi14 <= 100
    assert snapshot.trend in {"strong uptrend", "uptrend", "range / mixed"}


def test_snapshot_serialises_without_nan():
    """NaN is not valid JSON — a NaN here would break every prompt."""
    snapshot = build_snapshot("AAPL", make_candles([100.0 + i for i in range(30)]))
    assert snapshot is not None

    for key, value in snapshot.to_prompt_dict().items():
        if isinstance(value, float):
            assert not math.isnan(value), f"{key} is NaN"
            assert not math.isinf(value), f"{key} is infinite"
