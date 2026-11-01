"""
Technical indicators, implemented directly on pandas.

Written out rather than pulled from TA-Lib/pandas-ta on purpose: those are the
dependencies most likely to fail to build on a fresh machine, and these are
twenty lines each.
"""
from __future__ import annotations

from datetime import timezone

import numpy as np
import pandas as pd

from src.models import Candle, TechnicalSnapshot


def candles_to_frame(candles: list[Candle]) -> pd.DataFrame:
    df = pd.DataFrame(
        [
            {
                "ts": c.ts,
                "open": c.open,
                "high": c.high,
                "low": c.low,
                "close": c.close,
                "volume": c.volume,
            }
            for c in candles
        ]
    )
    if df.empty:
        return df
    return df.sort_values("ts").reset_index(drop=True)


def sma(series: pd.Series, period: int) -> pd.Series:
    return series.rolling(window=period, min_periods=period).mean()


def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False, min_periods=period).mean()


def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    # Wilder's smoothing.
    avg_gain = gain.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    out = 100 - (100 / (1 + rs))
    # All-gain windows have no loss to divide by; that is RSI 100, not NaN.
    return out.where(avg_loss.ne(0.0) | avg_gain.eq(0.0), 100.0)


def true_range(df: pd.DataFrame) -> pd.Series:
    prev_close = df["close"].shift(1)
    return pd.concat(
        [
            df["high"] - df["low"],
            (df["high"] - prev_close).abs(),
            (df["low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    return true_range(df).ewm(alpha=1 / period, adjust=False, min_periods=period).mean()


def macd(
    series: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9
) -> tuple[pd.Series, pd.Series, pd.Series]:
    macd_line = ema(series, fast) - ema(series, slow)
    signal_line = macd_line.ewm(span=signal, adjust=False, min_periods=signal).mean()
    return macd_line, signal_line, macd_line - signal_line


def bollinger(
    series: pd.Series, period: int = 20, std: float = 2.0
) -> tuple[pd.Series, pd.Series, pd.Series]:
    mid = sma(series, period)
    dev = series.rolling(window=period, min_periods=period).std(ddof=0)
    return mid + std * dev, mid, mid - std * dev


def swing_levels(df: pd.DataFrame, lookback: int = 60, pivot: int = 3) -> tuple[list[float], list[float]]:
    """
    Pivot highs/lows: a bar whose high (low) is the most extreme within +/- `pivot`
    bars. Cheap, and it produces the levels a human would actually draw.
    """
    window = df.tail(lookback).reset_index(drop=True)
    if len(window) < pivot * 2 + 1:
        return [], []

    highs, lows = [], []
    for i in range(pivot, len(window) - pivot):
        seg = window.iloc[i - pivot : i + pivot + 1]
        if window.at[i, "high"] == seg["high"].max():
            highs.append(float(window.at[i, "high"]))
        if window.at[i, "low"] == seg["low"].min():
            lows.append(float(window.at[i, "low"]))

    last = float(window["close"].iloc[-1])
    resistance = sorted({round(h, 2) for h in highs if h > last})[:4]
    support = sorted({round(l, 2) for l in lows if l < last}, reverse=True)[:4]
    return support, resistance


def classify_trend(close: float, s20: float, s50: float, s200: float) -> str:
    vals = [s20, s50, s200]
    if any(pd.isna(v) for v in vals):
        # Not enough history for the long averages — fall back to what we have.
        if not pd.isna(s20):
            return "uptrend" if close > s20 else "downtrend"
        return "unknown"
    if close > s20 > s50 > s200:
        return "strong uptrend"
    if close < s20 < s50 < s200:
        return "strong downtrend"
    if close > s50 and close > s200:
        return "uptrend"
    if close < s50 and close < s200:
        return "downtrend"
    return "range / mixed"


def _f(value) -> float:
    """NaN-safe float for JSON serialisation into a prompt."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return 0.0
    return 0.0 if (np.isnan(v) or np.isinf(v)) else v


def build_snapshot(symbol: str, candles: list[Candle]) -> TechnicalSnapshot | None:
    """Collapse an OHLCV history into the blob handed to the AI. None if too short."""
    df = candles_to_frame(candles)
    if df.empty or len(df) < 25:
        return None

    close = df["close"]
    s20, s50, s200 = sma(close, 20), sma(close, 50), sma(close, 200)
    e9, e21 = ema(close, 9), ema(close, 21)
    r = rsi(close, 14)
    a = atr(df, 14)
    macd_line, macd_sig, macd_hist = macd(close)
    bb_up, _, bb_low = bollinger(close)
    avg_vol = df["volume"].rolling(20, min_periods=5).mean()
    support, resistance = swing_levels(df)

    last = _f(close.iloc[-1])
    prev = _f(close.iloc[-2])
    upper, lower = _f(bb_up.iloc[-1]), _f(bb_low.iloc[-1])
    band = upper - lower
    atr_val = _f(a.iloc[-1])
    avg_vol_val = _f(avg_vol.iloc[-1])
    ts = df["ts"].iloc[-1]
    if getattr(ts, "tzinfo", None) is None:
        ts = ts.replace(tzinfo=timezone.utc)

    return TechnicalSnapshot(
        symbol=symbol,
        as_of=ts,
        last_price=last,
        prev_close=prev,
        change_pct=((last - prev) / prev * 100) if prev else 0.0,
        sma20=_f(s20.iloc[-1]),
        sma50=_f(s50.iloc[-1]),
        sma200=_f(s200.iloc[-1]),
        ema9=_f(e9.iloc[-1]),
        ema21=_f(e21.iloc[-1]),
        rsi14=_f(r.iloc[-1]),
        atr14=atr_val,
        atr_pct=(atr_val / last * 100) if last else 0.0,
        macd=_f(macd_line.iloc[-1]),
        macd_signal=_f(macd_sig.iloc[-1]),
        macd_hist=_f(macd_hist.iloc[-1]),
        bb_upper=upper,
        bb_lower=lower,
        bb_pct=((last - lower) / band * 100) if band > 0 else 50.0,
        volume=_f(df["volume"].iloc[-1]),
        avg_volume20=avg_vol_val,
        rel_volume=(_f(df["volume"].iloc[-1]) / avg_vol_val) if avg_vol_val else 0.0,
        trend=classify_trend(last, s20.iloc[-1], s50.iloc[-1], s200.iloc[-1]),
        support_levels=support,
        resistance_levels=resistance,
        range_high_20=_f(df["high"].tail(20).max()),
        range_low_20=_f(df["low"].tail(20).min()),
    )
