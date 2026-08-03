"""
Backtester — how the strategy rules would have done on past data.

Deliberately mechanical: it replays the strategies and the risk/sizing rules,
not the AI. You cannot reproduce what a model would have said six months ago,
and pretending otherwise produces a backtest that means nothing. So this
measures the part that *is* reproducible — the rules — and the AI layer is
tested forward, on paper.

Two things are guarded against:

* Lookahead bias. A decision at bar i is made from bars 0..i and filled at bar
  i+1's open. Nothing ever sees its own future.
* Same-bar ambiguity. When a bar's range contains both the stop and the target,
  the stop is assumed to hit first. That is not always true, but the honest
  direction to be wrong in is the pessimistic one.
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
            f"Bars tested    {self.bars_tested}",
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
            "This tests the mechanical rules only — no AI, no slippage, no",
            "commissions, no partial fills. Results here are an upper bound,",
            "not an expectation. If it looks too good, it is curve-fitted:",
            "re-run on data the rules were not designed against.",
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
    ) -> None:
        self.settings = settings
        self.market_data = market_data
        self.strategies = strategies
        self.starting_equity = starting_equity

    def run(self, symbols: list[str], bars: int = 500) -> BacktestResult:
        equity = self.starting_equity
        result = BacktestResult(
            starting_equity=equity, ending_equity=equity, symbols=list(symbols)
        )

        for symbol in symbols:
            try:
                candles = self.market_data.get_candles(symbol, "1Day", limit=bars)
            except MarketDataError as exc:
                result.skipped[symbol] = str(exc)
                log.warning("backtest.skip", symbol=symbol, error=str(exc))
                continue

            if len(candles) < MIN_BARS_BEFORE_TRADING + 10:
                result.skipped[symbol] = (
                    f"only {len(candles)} bars; need {MIN_BARS_BEFORE_TRADING + 10}"
                )
                continue

            equity = self._run_symbol(symbol, candles, equity, result)

        result.ending_equity = equity
        result.metrics = compute_metrics(
            [t.as_journal_row() for t in result.trades], result.equity_curve
        )
        return result

    # ------------------------------------------------------------------ #
    def _run_symbol(
        self, symbol: str, candles: list[Candle], equity: float, result: BacktestResult
    ) -> float:
        open_trade: dict | None = None

        for i in range(MIN_BARS_BEFORE_TRADING, len(candles) - 1):
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
            fill = candles[i + 1]
            open_trade = {
                "symbol": symbol,
                "strategy": setup.strategy,
                "direction": setup.direction,
                "opened_at": fill.ts,
                "qty": sizing.shares,
                "entry": fill.open,
                "stop": signal.stop,
                "target": signal.target,
            }

        # Close anything still open at the last bar, so it counts.
        if open_trade is not None:
            last = candles[-1]
            trade = self._close(open_trade, last.ts, last.close, "end of test window")
            equity += trade.pnl
            result.trades.append(trade)
            result.equity_curve.append({"ts": last.ts.isoformat(), "equity": equity})

        return equity

    @staticmethod
    def _check_exit(trade: dict, bar: Candle) -> tuple[float | None, str]:
        if trade["direction"] == "long":
            if bar.low <= trade["stop"]:
                return trade["stop"], "stop"
            if bar.high >= trade["target"]:
                return trade["target"], "target"
        else:
            if bar.high >= trade["stop"]:
                return trade["stop"], "stop"
            if bar.low <= trade["target"]:
                return trade["target"], "target"
        return None, ""

    @staticmethod
    def _close(trade: dict, ts: datetime, exit_price: float, reason: str) -> BacktestTrade:
        qty, entry, stop = trade["qty"], trade["entry"], trade["stop"]
        pnl = (
            (exit_price - entry) * qty
            if trade["direction"] == "long"
            else (entry - exit_price) * qty
        )
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
        )
