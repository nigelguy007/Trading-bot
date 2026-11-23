"""
Backtester — how the strategy rules would have done on past data.

Deliberately mechanical: it replays the strategies and the risk/sizing rules,
not the AI. You cannot reproduce what a model would have said six months ago,
and pretending otherwise produces a backtest that means nothing. So this
measures the part that *is* reproducible — the rules — and the AI layer is
tested forward, on paper.

Four things are guarded against:

* Lookahead bias. A decision at bar i is made from bars 0..i and filled at bar
  i+1's open. Nothing ever sees its own future.
* Same-bar ambiguity. When a bar's range contains both the stop and the target,
  the stop is assumed to hit first. That is not always true, but the honest
  direction to be wrong in is the pessimistic one.
* Gaps. A bar that OPENS through the stop fills at the open, not at the stop.
  A stop is a market order once touched; nobody gets the stop price on a gap.
* Free trading. Commission and slippage come from a CostModel. Market orders
  (entries, stops, end-of-window exits) are slipped against you; the target is
  a resting limit order, so it fills at its price or better and is not slipped.

A date window (`start`/`end`) restricts where trades may be OPENED and closes
anything still open at the window's last bar. Bars before `start` are still
used for indicator warm-up — that is past data, so it is not lookahead.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from src.config import Settings
from src.data.indicators import build_snapshot
from src.data.market_data import MarketDataError, MarketDataProvider
from src.logging_setup import get_logger
from src.metrics import PerformanceMetrics, compute_metrics
from src.models import Candle, Signal
from src.sizing import calculate_size
from strategies.base import Strategy

log = get_logger(__name__)

MIN_BARS_BEFORE_TRADING = 200  # let the 200 SMA warm up before anything fires
ALL_BARS = 10**9  # "give me everything" for date-windowed runs


@dataclass(frozen=True)
class CostModel:
    """
    What a round trip really costs.

    commission_per_unit  flat fee per share/contract per side (e.g. 2.25 for an
                         ES contract all-in, 0.005 for IBKR shares)
    commission_pct       fee as a % of notional per side (e.g. 0.1 for crypto)
    slippage_bps         adverse fill vs the reference price on MARKET orders,
                         in basis points (1 bp = 0.01%)
    """

    commission_per_unit: float = 0.0
    commission_pct: float = 0.0
    slippage_bps: float = 0.0

    def __post_init__(self) -> None:
        for name in ("commission_per_unit", "commission_pct", "slippage_bps"):
            if getattr(self, name) < 0:
                raise ValueError(f"{name} cannot be negative")

    @property
    def is_free(self) -> bool:
        return not (self.commission_per_unit or self.commission_pct or self.slippage_bps)

    def slip(self, price: float, direction: str, opening: bool) -> float:
        """Move a market-order fill against the trader."""
        frac = self.slippage_bps / 10_000
        buying = (direction == "long") == opening
        return price * (1 + frac) if buying else price * (1 - frac)

    def commission(self, qty: float, price: float) -> float:
        """One side's commission."""
        return abs(qty) * self.commission_per_unit + abs(qty) * price * self.commission_pct / 100

    def describe(self) -> str:
        if self.is_free:
            return "none (frictionless — results are an upper bound)"
        return (
            f"{self.commission_per_unit:g}/unit + {self.commission_pct:g}% per side, "
            f"{self.slippage_bps:g} bp slippage on market orders"
        )


@dataclass
class BacktestTrade:
    symbol: str
    strategy: str
    direction: str
    opened_at: datetime
    closed_at: datetime
    qty: float
    entry: float
    exit: float
    stop: float
    target: float
    pnl: float
    r_multiple: float
    exit_reason: str
    costs: float = 0.0
    status: str = "closed"

    def as_journal_row(self) -> dict:
        return {
            "symbol": self.symbol,
            "strategy": self.strategy,
            "direction": self.direction,
            "opened_at": self.opened_at.isoformat(),
            "closed_at": self.closed_at.isoformat(),
            "qty": self.qty,
            "entry": self.entry,
            "exit": self.exit,
            "stop": self.stop,
            "target": self.target,
            "pnl": self.pnl,
            "r_multiple": self.r_multiple,
            "exit_reason": self.exit_reason,
            "fees": self.costs,
            "status": "closed",
        }


@dataclass
class BacktestResult:
    starting_equity: float
    ending_equity: float
    trades: list[BacktestTrade] = field(default_factory=list)
    equity_curve: list[dict] = field(default_factory=list)
    metrics: PerformanceMetrics = field(default_factory=PerformanceMetrics)
    symbols: list[str] = field(default_factory=list)
    bars_tested: int = 0
    skipped: dict[str, str] = field(default_factory=dict)
    window: str = "all available bars"
    cost_model: CostModel = field(default_factory=CostModel)

    @property
    def total_costs(self) -> float:
        return sum(t.costs for t in self.trades)

    @property
    def return_pct(self) -> float:
        if not self.starting_equity:
            return 0.0
        return (self.ending_equity - self.starting_equity) / self.starting_equity * 100

    def report(self) -> str:
        lines = [
            "=" * 58,
            "BACKTEST RESULT",
            "=" * 58,
            f"Symbols        {', '.join(self.symbols) or 'none'}",
            f"Window         {self.window}",
            f"Bars tested    {self.bars_tested}",
            f"Costs model    {self.cost_model.describe()}",
            f"Costs paid     ${self.total_costs:,.2f}",
            f"Start equity   ${self.starting_equity:,.2f}",
            f"End equity     ${self.ending_equity:,.2f}",
            f"Return         {self.return_pct:+.2f}%",
            "",
            self.metrics.summary(),
        ]
        if self.metrics.by_strategy:
            lines += ["", "BY STRATEGY"]
            for name, stats in sorted(self.metrics.by_strategy.items()):
                lines.append(
                    f"  {name:<18} {stats['trades']:>4} trades  "
                    f"win {stats['win_rate']:>5.1f}%  avg {stats['avg_r']:+.2f}R  "
                    f"${stats['total_pnl']:+,.2f}"
                )
        if self.skipped:
            lines += ["", "SKIPPED"]
            lines += [f"  {sym}: {why}" for sym, why in self.skipped.items()]

        lines += [
            "",
            "-" * 58,
            "This tests the mechanical rules only — no AI, no partial fills.",
            "Results are an upper bound, not an expectation. If it looks too",
            "good, it is curve-fitted: judge it on out-of-sample data only.",
            "=" * 58,
        ]
        return "\n".join(lines)


class Backtester:
    def __init__(
        self,
        settings: Settings,
        market_data: MarketDataProvider,
        strategies: list[Strategy],
        starting_equity: float = 10_000.0,
        costs: CostModel | None = None,
    ) -> None:
        self.settings = settings
        self.market_data = market_data
        self.strategies = strategies
        self.starting_equity = starting_equity
        self.costs = costs or CostModel()

    def run(
        self,
        symbols: list[str],
        bars: int = 500,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> BacktestResult:
        """
        Backtest `symbols`. With no window, the last `bars` bars are used. With
        `start`/`end`, every bar is loaded and trades may only open inside
        [start, end) — `end` is exclusive so 2010→2020 and 2020→2026 never share
        a bar.
        """
        equity = self.starting_equity
        windowed = start is not None or end is not None
        result = BacktestResult(
            starting_equity=equity,
            ending_equity=equity,
            symbols=list(symbols),
            window=_describe_window(start, end) if windowed else f"last {bars} bars",
            cost_model=self.costs,
        )

        for symbol in symbols:
            try:
                candles = self.market_data.get_candles(
                    symbol, "1Day", limit=ALL_BARS if windowed else bars
                )
            except MarketDataError as exc:
                result.skipped[symbol] = str(exc)
                log.warning("backtest.skip", symbol=symbol, error=str(exc))
                continue

            first, last = _window_indices(candles, start, end)
            first = max(first, MIN_BARS_BEFORE_TRADING)
            if last - first < 10:
                result.skipped[symbol] = (
                    f"only {max(last - first, 0)} tradable bars in the window "
                    f"(need {MIN_BARS_BEFORE_TRADING} warm-up bars before it, "
                    f"{len(candles)} loaded)"
                )
                continue

            equity = self._run_symbol(symbol, candles, equity, result, first, last)

        result.ending_equity = equity
        result.metrics = compute_metrics(
            [t.as_journal_row() for t in result.trades], result.equity_curve
        )
        return result

    # ------------------------------------------------------------------ #
    def _run_symbol(
        self,
        symbol: str,
        candles: list[Candle],
        equity: float,
        result: BacktestResult,
        first: int,
        last: int,
    ) -> float:
        """Trade bars first..last (inclusive); every fill stays inside the window."""
        open_trade: dict | None = None

        for i in range(first, last):
            bar = candles[i]
            result.bars_tested += 1

            # --- manage an open position on this bar --------------------- #
            if open_trade is not None:
                exit_price, reason = self._check_exit(open_trade, bar)
                if exit_price is not None:
                    trade = self._close(open_trade, bar.ts, exit_price, reason)
                    equity += trade.pnl
                    result.trades.append(trade)
                    result.equity_curve.append({"ts": bar.ts.isoformat(), "equity": equity})
                    open_trade = None
                else:
                    continue  # one position per symbol at a time

            # --- look for a new setup using bars 0..i only --------------- #
            snapshot = build_snapshot(symbol, candles[: i + 1])
            if snapshot is None:
                continue

            setup = next(
                (s for s in (st.evaluate(snapshot) for st in self.strategies) if s), None
            )
            if setup is None:
                continue

            signal = Signal(
                symbol=symbol,
                direction=setup.direction,
                entry=setup.entry,
                stop=setup.stop,
                target=setup.target,
                confidence=1.0,  # mechanical rules carry no model confidence
                reasoning=setup.rationale,
                source=setup.strategy,
                asset_class="crypto" if "/" in symbol else "equity",
            )
            if not signal.stop_is_coherent() or signal.risk_reward < self.settings.min_risk_reward:
                continue

            sizing = calculate_size(signal, equity, self.settings)
            if sizing.shares <= 0:
                continue

            # Fill at the NEXT bar's open — the decision bar is already closed.
            # A market order, so it is slipped against us.
            fill = candles[i + 1]
            entry = self.costs.slip(fill.open, setup.direction, opening=True)
            # If the open already gapped through the stop, the plan is void.
            if (setup.direction == "long" and entry <= signal.stop) or (
                setup.direction == "short" and entry >= signal.stop
            ):
                continue
            open_trade = {
                "symbol": symbol,
                "strategy": setup.strategy,
                "direction": setup.direction,
                "opened_at": fill.ts,
                "qty": sizing.shares,
                "entry": entry,
                "stop": signal.stop,
                "target": signal.target,
            }

        # Close anything still open at the window's last bar, so it counts.
        if open_trade is not None:
            bar = candles[last]
            exit_price = self.costs.slip(bar.close, open_trade["direction"], opening=False)
            trade = self._close(open_trade, bar.ts, exit_price, "end of test window")
            equity += trade.pnl
            result.trades.append(trade)
            result.equity_curve.append({"ts": bar.ts.isoformat(), "equity": equity})

        return equity

    def _check_exit(self, trade: dict, bar: Candle) -> tuple[float | None, str]:
        return check_exit(trade["direction"], trade["stop"], trade["target"], bar, self.costs)

    def _close(self, trade: dict, ts: datetime, exit_price: float, reason: str) -> BacktestTrade:
        qty, entry, stop = trade["qty"], trade["entry"], trade["stop"]
        gross = (
            (exit_price - entry) * qty
            if trade["direction"] == "long"
            else (entry - exit_price) * qty
        )
        costs = self.costs.commission(qty, entry) + self.costs.commission(qty, exit_price)
        pnl = gross - costs
        risk_per_share = abs(entry - stop)
        return BacktestTrade(
            symbol=trade["symbol"],
            strategy=trade["strategy"],
            direction=trade["direction"],
            opened_at=trade["opened_at"],
            closed_at=ts,
            qty=qty,
            entry=entry,
            exit=exit_price,
            stop=stop,
            target=trade["target"],
            pnl=pnl,
            r_multiple=(pnl / (risk_per_share * qty)) if risk_per_share and qty else 0.0,
            exit_reason=reason,
            costs=costs,
        )


def check_exit(
    direction: str, stop: float, target: float, bar: Candle, costs: CostModel
) -> tuple[float | None, str]:
    """
    Did this bar take the trade out, and at what price?

    Order of checks matters and is pessimistic on purpose: a gap through the
    stop fills at the open; a bar that touches both levels is a stop.
    """
    if direction == "long":
        if bar.open <= stop:
            return costs.slip(bar.open, direction, opening=False), "stop (gap)"
        if bar.low <= stop:
            return costs.slip(stop, direction, opening=False), "stop"
        if bar.open >= target:
            return bar.open, "target (gap)"  # a resting limit fills at the better open
        if bar.high >= target:
            return target, "target"
    else:
        if bar.open >= stop:
            return costs.slip(bar.open, direction, opening=False), "stop (gap)"
        if bar.high >= stop:
            return costs.slip(stop, direction, opening=False), "stop"
        if bar.open <= target:
            return bar.open, "target (gap)"
        if bar.low <= target:
            return target, "target"
    return None, ""


def _window_indices(
    candles: list[Candle], start: datetime | None, end: datetime | None
) -> tuple[int, int]:
    """Index of the first bar >= start and the last bar < end (inclusive)."""
    first = 0
    if start is not None:
        first = next((i for i, c in enumerate(candles) if c.ts >= start), len(candles))
    last = len(candles) - 1
    if end is not None:
        last = next((i for i, c in enumerate(candles) if c.ts >= end), len(candles)) - 1
    return first, last


def _describe_window(start: datetime | None, end: datetime | None) -> str:
    left = start.date().isoformat() if start else "first bar"
    right = end.date().isoformat() if end else "last bar"
    return f"{left} → {right} (end exclusive)"
