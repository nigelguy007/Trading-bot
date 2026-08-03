"""
Performance metrics: win rate, average R, max drawdown, profit factor.

Average R is the one that matters most. A 40% win rate with +2R winners beats a
70% win rate with -3R losers, and only R-multiples make that visible — raw P&L
hides it behind position size.

Every metric degrades gracefully on a thin sample, and `is_significant` exists
so a report can say "12 trades" rather than implying 12 trades proved anything.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

MIN_MEANINGFUL_TRADES = 30  # the guide's paper-trading bar before going live


@dataclass
class PerformanceMetrics:
    total_trades: int = 0
    wins: int = 0
    losses: int = 0
    breakeven: int = 0
    win_rate: float = 0.0
    total_pnl: float = 0.0
    avg_win: float = 0.0
    avg_loss: float = 0.0
    avg_r: float = 0.0
    best_r: float = 0.0
    worst_r: float = 0.0
    profit_factor: float = 0.0
    expectancy_r: float = 0.0
    max_drawdown_pct: float = 0.0
    sharpe: float = 0.0
    by_strategy: dict[str, dict[str, Any]] = field(default_factory=dict)

    @property
    def is_significant(self) -> bool:
        return self.total_trades >= MIN_MEANINGFUL_TRADES

    def summary(self) -> str:
        if not self.total_trades:
            return "No closed trades yet."
        caveat = "" if self.is_significant else (
            f"  (only {self.total_trades} trades — below the {MIN_MEANINGFUL_TRADES}-trade "
            "bar for drawing conclusions)"
        )
        return (
            f"{self.total_trades} trades | win rate {self.win_rate:.1f}% | "
            f"avg {self.avg_r:+.2f}R | expectancy {self.expectancy_r:+.2f}R | "
            f"profit factor {self.profit_factor:.2f} | max DD {self.max_drawdown_pct:.1f}% | "
            f"P&L ${self.total_pnl:+,.2f}{caveat}"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_trades": self.total_trades,
            "wins": self.wins,
            "losses": self.losses,
            "win_rate": round(self.win_rate, 2),
            "total_pnl": round(self.total_pnl, 2),
            "avg_win": round(self.avg_win, 2),
            "avg_loss": round(self.avg_loss, 2),
            "avg_r": round(self.avg_r, 3),
            "best_r": round(self.best_r, 2),
            "worst_r": round(self.worst_r, 2),
            "profit_factor": round(self.profit_factor, 3),
            "expectancy_r": round(self.expectancy_r, 3),
            "max_drawdown_pct": round(self.max_drawdown_pct, 2),
            "sharpe": round(self.sharpe, 3),
            "is_significant": self.is_significant,
            "by_strategy": self.by_strategy,
        }


def compute_metrics(
    trades: list[dict], equity_curve: list[dict] | None = None
) -> PerformanceMetrics:
    closed = [t for t in trades if t.get("status") == "closed" and t.get("pnl") is not None]
    m = PerformanceMetrics()
    if not closed:
        return m

    pnls = [float(t["pnl"]) for t in closed]
    r_values = [float(t.get("r_multiple") or 0.0) for t in closed]

    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]

    m.total_trades = len(closed)
    m.wins, m.losses = len(wins), len(losses)
    m.breakeven = m.total_trades - m.wins - m.losses
    m.win_rate = (m.wins / m.total_trades * 100) if m.total_trades else 0.0
    m.total_pnl = sum(pnls)
    m.avg_win = (sum(wins) / len(wins)) if wins else 0.0
    m.avg_loss = (sum(losses) / len(losses)) if losses else 0.0
    m.avg_r = (sum(r_values) / len(r_values)) if r_values else 0.0
    m.best_r = max(r_values) if r_values else 0.0
    m.worst_r = min(r_values) if r_values else 0.0

    gross_profit, gross_loss = sum(wins), abs(sum(losses))
    # No losers yet isn't an infinite profit factor, it's an unfinished sample.
    m.profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else (
        float("inf") if gross_profit > 0 and m.total_trades >= MIN_MEANINGFUL_TRADES else 0.0
    )

    win_rate = m.win_rate / 100
    avg_win_r = sum(r for r in r_values if r > 0) / max(len([r for r in r_values if r > 0]), 1)
    avg_loss_r = abs(sum(r for r in r_values if r < 0)) / max(len([r for r in r_values if r < 0]), 1)
    m.expectancy_r = win_rate * avg_win_r - (1 - win_rate) * avg_loss_r

    m.max_drawdown_pct = _max_drawdown(equity_curve) if equity_curve else _max_drawdown_from_pnl(pnls)
    m.sharpe = _sharpe(r_values)
    m.by_strategy = _by_strategy(closed)
    return m


def _by_strategy(closed: list[dict]) -> dict[str, dict[str, Any]]:
    groups: dict[str, list[dict]] = {}
    for t in closed:
        groups.setdefault(t.get("strategy") or "unknown", []).append(t)

    out = {}
    for name, group in groups.items():
        pnls = [float(t["pnl"]) for t in group]
        rs = [float(t.get("r_multiple") or 0) for t in group]
        wins = len([p for p in pnls if p > 0])
        out[name] = {
            "trades": len(group),
            "win_rate": round(wins / len(group) * 100, 1),
            "total_pnl": round(sum(pnls), 2),
            "avg_r": round(sum(rs) / len(rs), 3) if rs else 0.0,
        }
    return out


def _max_drawdown(equity_curve: list[dict]) -> float:
    peak, max_dd = 0.0, 0.0
    for point in equity_curve:
        equity = float(point.get("equity") or 0)
        peak = max(peak, equity)
        if peak > 0:
            max_dd = max(max_dd, (peak - equity) / peak * 100)
    return max_dd


def _max_drawdown_from_pnl(pnls: list[float]) -> float:
    """Fallback when there's no equity curve: drawdown of the cumulative P&L."""
    cumulative, peak, max_dd = 0.0, 0.0, 0.0
    for pnl in reversed(pnls):  # journal returns newest-first
        cumulative += pnl
        peak = max(peak, cumulative)
        if peak > 0:
            max_dd = max(max_dd, (peak - cumulative) / peak * 100)
    return max_dd


def _sharpe(r_values: list[float]) -> float:
    """Sharpe over per-trade R. Not annualised — it compares strategies, nothing more."""
    if len(r_values) < 2:
        return 0.0
    mean = sum(r_values) / len(r_values)
    variance = sum((r - mean) ** 2 for r in r_values) / (len(r_values) - 1)
    std = math.sqrt(variance)
    return (mean / std) if std > 0 else 0.0
