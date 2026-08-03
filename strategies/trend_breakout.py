"""
Trend breakout: buy strength that is actually confirmed.

Rules — all must hold:
  * Price above the 20/50 SMAs (trend is up).
  * Close within 1% of the 20-day high (breaking out, not chasing).
  * Relative volume >= 1.2 (someone is participating).
  * RSI between 50 and 78 (momentum present, not exhausted).

Stop goes 1.5 ATR below entry; target is 2.5 ATR, giving a structural R:R of
about 1.67 before any risk-gate filtering.
"""
from __future__ import annotations

from typing import Optional

from src.models import StrategySetup, TechnicalSnapshot
from strategies.base import Strategy

STOP_ATR_MULT = 1.5
TARGET_ATR_MULT = 2.5


class TrendBreakout(Strategy):
    name = "trend_breakout"
    timeframe = "swing"

    def evaluate(self, snapshot: TechnicalSnapshot) -> Optional[StrategySetup]:
        s = snapshot
        if s.atr14 <= 0 or s.last_price <= 0 or s.range_high_20 <= 0:
            return None
        if s.sma20 <= 0 or s.sma50 <= 0:
            return None  # not enough history to judge trend

        uptrend = s.last_price > s.sma20 > s.sma50
        near_high = s.last_price >= s.range_high_20 * 0.99
        volume_ok = s.rel_volume >= 1.2
        momentum_ok = 50 <= s.rsi14 <= 78

        if not (uptrend and near_high and volume_ok and momentum_ok):
            return None

        entry = s.last_price
        return StrategySetup(
            strategy=self.name,
            symbol=s.symbol,
            direction="long",
            entry=round(entry, 2),
            stop=round(entry - STOP_ATR_MULT * s.atr14, 2),
            target=round(entry + TARGET_ATR_MULT * s.atr14, 2),
            timeframe=self.timeframe,
            rationale=(
                f"{s.trend}; price {s.last_price:.2f} breaking the 20-day high "
                f"{s.range_high_20:.2f} on {s.rel_volume:.1f}x volume, RSI {s.rsi14:.0f}. "
                f"Stop {STOP_ATR_MULT}xATR ({s.atr14:.2f}) below entry."
            ),
        )
