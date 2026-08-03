"""
The daily report: what happened, and what the running performance looks like.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src.journal import TradeJournal
from src.metrics import PerformanceMetrics, compute_metrics
from src.portfolio import PortfolioState


def build_daily_report(
    journal: TradeJournal,
    state: PortfolioState,
    *,
    paper: bool = True,
    execution_mode: str = "alert",
) -> str:
    since = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    todays_signals = journal.signals_since(since)
    metrics = compute_metrics(journal.closed_trades(limit=1000), journal.equity_curve())

    approved = [s for s in todays_signals if s["risk_approved"]]
    rejected = [s for s in todays_signals if not s["risk_approved"]]
    submitted = [s for s in todays_signals if s["status"] == "submitted"]
    pending = [s for s in todays_signals if s["status"] == "pending_approval"]

    lines = [
        "=" * 58,
        f"DAILY REPORT — {datetime.now(timezone.utc):%Y-%m-%d}",
        f"Mode: {'PAPER' if paper else 'LIVE'} / {execution_mode}",
        "=" * 58,
        "",
        "ACCOUNT",
        f"  Equity        ${state.account.equity:,.2f}",
        f"  Day P&L       {state.day_pnl_pct:+.2f}%  (${state.account.day_pnl:+,.2f})",
        f"  Drawdown      -{state.drawdown_pct:.2f}% from ${state.peak_equity:,.2f} peak",
        f"  Exposure      ${state.exposure:,.2f} ({state.exposure_pct:.1f}%)",
        f"  Open risk     ${state.open_risk:,.2f} ({state.open_risk_pct:.2f}%)",
        "",
        "TODAY",
        f"  Signals generated   {len(todays_signals)}",
        f"  Passed risk gate    {len(approved)}",
        f"  Blocked by risk     {len(rejected)}",
        f"  Orders submitted    {len(submitted)}",
        f"  Awaiting approval   {len(pending)}",
    ]

    if rejected:
        lines += ["", "WHY SETUPS WERE BLOCKED"]
        counts: dict[str, int] = {}
        for s in rejected:
            for reason in (s["risk_reason"] or "").split(";"):
                reason = reason.strip()
                if reason:
                    counts[reason] = counts.get(reason, 0) + 1
        for reason, n in sorted(counts.items(), key=lambda kv: -kv[1])[:6]:
            lines.append(f"  {n}x {reason}")

    if state.positions:
        lines += ["", "OPEN POSITIONS"]
        for p in state.positions:
            lines.append(
                f"  {p.symbol:<10} {p.qty:>10g} @ {p.avg_entry:>9,.2f} "
                f"now {p.market_price:>9,.2f}  {p.unrealized_pnl:+,.2f}"
            )

    lines += ["", "PERFORMANCE (all closed trades)", f"  {metrics.summary()}"]

    if metrics.by_strategy:
        lines += ["", "BY STRATEGY"]
        for name, stats in sorted(metrics.by_strategy.items()):
            lines.append(
                f"  {name:<18} {stats['trades']:>4} trades  "
                f"win {stats['win_rate']:>5.1f}%  avg {stats['avg_r']:+.2f}R  "
                f"${stats['total_pnl']:+,.2f}"
            )

    lines += ["", _readiness_note(metrics, paper), "=" * 58]
    return "\n".join(lines)


def _readiness_note(metrics: PerformanceMetrics, paper: bool) -> str:
    if not paper:
        return "Running LIVE. Review every signal; the AI can be confidently wrong."
    if not metrics.is_significant:
        return (
            f"Still paper trading — {metrics.total_trades} closed trades. "
            "The bar before even considering live is a few weeks and 30+ trades, "
            "then tiny size."
        )
    if metrics.expectancy_r <= 0:
        return (
            "30+ trades with a non-positive expectancy. The edge isn't there yet — "
            "going live now would just lose money faster."
        )
    return (
        f"{metrics.total_trades} trades at {metrics.expectancy_r:+.2f}R expectancy. "
        "If that holds over more weeks of forward testing, consider live with tiny size."
    )
