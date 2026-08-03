"""
Mean reversion: buy a stretched pullback inside an intact uptrend.

Rules — all must hold:
  * Price above the 200 SMA (only fade dips in things trending up; fading a
    downtrend is how accounts die).
  * RSI below 32 (genuinely oversold).
  * Price at or below the lower Bollinger band.
  * Price still within 12% of the 50 SMA (a pullback, not a regime change).

Stop goes 1.2 ATR below entry; target is the 20 SMA, which is where the mean
actually is.
"""
from __future__ import annotations

from typing import Optional

from src.models import StrategySetup, TechnicalSnapshot
from strategies.base import Strategy

STOP_ATR_MULT = 1.2


class MeanReversion(Strategy):
    name = "mean_reversion"
    timeframe = "swing"

    def evaluate(self, snapshot: TechnicalSnapshot) -> Optional[StrategySetup]:
        s = snapshot
        if s.atr14 <= 0 or s.last_price <= 0 or s.sma200 <= 0 or s.sma20 <= 0:
            return None

        in_uptrend = s.last_price > s.sma200
        oversold = s.rsi14 < 32
        at_band = s.bb_lower > 0 and s.last_price <= s.bb_lower
        not_broken = s.sma50 > 0 and s.last_price >= s.sma50 * 0.88

        if not (in_uptrend and oversold and at_band and not_broken):
            return None

        entry = s.last_price
        target = s.sma20
        if target <= entry:
            return None  # mean is below price; there is nothing to revert to

        return StrategySetup(
            strategy=self.name,
            symbol=s.symbol,
            direction="long",
            entry=round(entry, 2),
            stop=round(entry - STOP_ATR_MULT * s.atr14, 2),
            target=round(target, 2),
            timeframe=self.timeframe,
            rationale=(
                f"Pullback inside an uptrend: price {s.last_price:.2f} above the "
                f"200SMA {s.sma200:.2f}, RSI {s.rsi14:.0f}, tagging the lower band "
                f"{s.bb_lower:.2f}. Target is the 20SMA at {target:.2f}."
            ),
        )
