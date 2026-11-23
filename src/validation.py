"""
Out-of-sample validation — the part of a backtest that can actually say no.

The workflow is the one every honest system builder uses:

  1. Build (design, tune, fiddle) on the IN-SAMPLE years only.
  2. Run ONCE on the OUT-OF-SAMPLE years the rules have never seen.
  3. Compare against a coin flip — random entries with the same stop/target
     geometry, the same costs and the same number of trades.

Step 3 is what "make sure it beats a coin flip" has to mean to be worth
anything. "Win rate above 50%" is not it: a strategy with a 2:1 target wins
less than half the time and still makes money, while a 70% win rate with a 1:5
payoff loses. The question is whether the strategy's ENTRIES add value over
random entries that face the same exits and the same friction.

The out-of-sample window is a one-shot resource. Every time you look at it and
then change the rules, it quietly becomes in-sample. The report says so.
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import datetime

from src.backtest import (
    ALL_BARS,
    Backtester,
    BacktestResult,
    BacktestTrade,
    CostModel,
    _window_indices,
    check_exit,
)
from src.data.market_data import MarketDataError, MarketDataProvider
from src.models import Candle

MIN_TRADES_TO_JUDGE = 30  # below this, any verdict is noise
SIGNIFICANCE = 0.05


@dataclass
class CoinFlipResult:
    strategy_total_r: float
    random_total_r: list[float] = field(default_factory=list)
    trades_matched: int = 0

    @property
    def simulations(self) -> int:
        return len(self.random_total_r)

    @property
    def p_value(self) -> float:
        """Chance a coin flip does at least as well. (k+1)/(n+1), never exactly 0."""
        if not self.random_total_r:
            return 1.0
        at_least = sum(1 for r in self.random_total_r if r >= self.strategy_total_r)
        return (at_least + 1) / (self.simulations + 1)

    @property
    def median_random_r(self) -> float:
        if not self.random_total_r:
            return 0.0
        ordered = sorted(self.random_total_r)
        return ordered[len(ordered) // 2]

    @property
    def beats_coin_flip(self) -> bool:
        return self.p_value < SIGNIFICANCE and self.strategy_total_r > 0


@dataclass
class ValidationReport:
    in_sample: BacktestResult
    out_of_sample: BacktestResult
    coin_flip: CoinFlipResult

    @property
    def verdict(self) -> str:
        oos = self.out_of_sample
        n = oos.metrics.total_trades
        if n < MIN_TRADES_TO_JUDGE:
            return (
                f"INCONCLUSIVE — only {n} out-of-sample trades (need "
                f"{MIN_TRADES_TO_JUDGE}+). Add symbols or a longer window; do NOT "
                "go live on this."
            )
        if oos.metrics.expectancy_r <= 0:
            return "FAIL — out-of-sample expectancy is not positive after costs."
        if not self.coin_flip.beats_coin_flip:
            return (
                f"FAIL — does not beat a coin flip (p = {self.coin_flip.p_value:.3f}). "
                "The profit is explainable by the stop/target geometry, not the entries."
            )
        return (
            f"PASS — positive out-of-sample expectancy after costs and beats a coin "
            f"flip (p = {self.coin_flip.p_value:.3f}). Next: weeks of PAPER trading."
        )

    @property
    def overfit_warning(self) -> str:
        ins, oos = self.in_sample.metrics, self.out_of_sample.metrics
        if ins.expectancy_r > 0 and oos.expectancy_r < ins.expectancy_r / 2:
            return (
                "WARNING: out-of-sample expectancy is under half of in-sample — "
                "classic overfitting."
            )
        return ""

    def report(self) -> str:
        ins, oos, cf = self.in_sample, self.out_of_sample, self.coin_flip
        im, om = ins.metrics, oos.metrics
        lines = [
            "=" * 70,
            "WALK-FORWARD VALIDATION",
            "=" * 70,
            f"{'':<18}{'IN-SAMPLE':>26}{'OUT-OF-SAMPLE':>26}",
            f"{'Window':<18}{_short(ins.window):>26}{_short(oos.window):>26}",
            _row("Trades", im.total_trades, om.total_trades, "{:d}"),
            _row("Win rate %", im.win_rate, om.win_rate, "{:.1f}"),
            _row("Expectancy (R)", im.expectancy_r, om.expectancy_r, "{:+.3f}"),
            _row("Profit factor", im.profit_factor, om.profit_factor, "{:.2f}"),
            _row("Max drawdown %", im.max_drawdown_pct, om.max_drawdown_pct, "{:.1f}"),
            _row("Return %", ins.return_pct, oos.return_pct, "{:+.1f}"),
            _row("Costs paid $", ins.total_costs, oos.total_costs, "{:,.0f}"),
            f"{'Costs model':<18}{ins.cost_model.describe()}",
            "",
            "COIN-FLIP BENCHMARK (out-of-sample)",
            f"  Strategy total     {cf.strategy_total_r:+.1f}R over {cf.trades_matched} trades",
            f"  Random median      {cf.median_random_r:+.1f}R  ({cf.simulations} simulations)",
            f"  p-value            {cf.p_value:.3f}  (chance random entries do as well)",
            "",
            "VERDICT",
            f"  {self.verdict}",
        ]
        if self.overfit_warning:
            lines.append(f"  {self.overfit_warning}")
        lines += [
            "",
            "-" * 70,
            "The out-of-sample window is now SPENT. If you change the rules",
            "after reading this, it is in-sample data and this test no longer",
            "means anything. Validate the next version on fresh data or paper.",
            "=" * 70,
        ]
        return "\n".join(lines)


def walk_forward(
    backtester: Backtester,
    symbols: list[str],
    start: datetime,
    split: datetime,
    end: datetime | None = None,
    simulations: int = 1000,
    seed: int = 42,
) -> ValidationReport:
    """Run in-sample [start, split) and out-of-sample [split, end), then the benchmark."""
    if not start < split or (end is not None and not split < end):
        raise ValueError("need start < split < end")

    in_sample = backtester.run(symbols, start=start, end=split)
    out_of_sample = backtester.run(symbols, start=split, end=end)
    coin_flip = coin_flip_benchmark(
        backtester.market_data,
        out_of_sample.trades,
        backtester.costs,
        split,
        end,
        simulations=simulations,
        seed=seed,
    )
    return ValidationReport(in_sample, out_of_sample, coin_flip)


def coin_flip_benchmark(
    market_data: MarketDataProvider,
    trades: list[BacktestTrade],
    costs: CostModel,
    start: datetime | None,
    end: datetime | None,
    simulations: int = 1000,
    seed: int = 42,
) -> CoinFlipResult:
    """
    Replace every real entry with a random one: random bar inside the window,
    random direction, the SAME stop and target distances (as % of entry), the
    same costs. Repeat `simulations` times and compare total R.
    """
    result = CoinFlipResult(strategy_total_r=sum(t.r_multiple for t in trades))
    if not trades:
        return result

    candles_by_symbol: dict[str, list[Candle]] = {}
    for symbol in sorted({t.symbol for t in trades}):
        try:
            candles = market_data.get_candles(symbol, "1Day", limit=ALL_BARS)
        except MarketDataError:
            continue
        first, last = _window_indices(candles, start, end)
        if last - first >= 2:
            candles_by_symbol[symbol] = candles[first : last + 1]

    templates = [
        (t.symbol, abs(t.entry - t.stop) / t.entry, abs(t.target - t.entry) / t.entry)
        for t in trades
        if t.symbol in candles_by_symbol and t.entry > 0 and t.entry != t.stop
    ]
    result.trades_matched = len(templates)
    if not templates:
        return result

    rng = random.Random(seed)
    for _ in range(simulations):
        total = 0.0
        for symbol, stop_pct, target_pct in templates:
            total += _random_trade_r(candles_by_symbol[symbol], stop_pct, target_pct, costs, rng)
        result.random_total_r.append(total)
    return result


def _random_trade_r(
    bars: list[Candle], stop_pct: float, target_pct: float, costs: CostModel, rng: random.Random
) -> float:
    """One random-entry trade, measured in R, with the same exit logic as the backtest."""
    i = rng.randrange(0, len(bars) - 1)
    direction = "long" if rng.random() < 0.5 else "short"
    raw = bars[i + 1].open
    entry = costs.slip(raw, direction, opening=True)
    sign = 1 if direction == "long" else -1
    stop = raw * (1 - sign * stop_pct)
    target = raw * (1 + sign * target_pct)

    exit_price = None
    for bar in bars[i + 2 :]:
        exit_price, _ = check_exit(direction, stop, target, bar, costs)
        if exit_price is not None:
            break
    if exit_price is None:
        exit_price = costs.slip(bars[-1].close, direction, opening=False)

    gross = (exit_price - entry) * sign
    net = gross - costs.commission(1, entry) - costs.commission(1, exit_price)
    risk = abs(entry - stop)
    return net / risk if risk else 0.0


def _row(label: str, a, b, fmt: str) -> str:
    return f"{label:<18}{fmt.format(a):>26}{fmt.format(b):>26}"


def _short(window: str) -> str:
    return window.replace(" (end exclusive)", "")
