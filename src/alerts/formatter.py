"""
Alert text.

An alert always carries the reasoning and the invalidation, not just the
levels. The point of the human gate is that you can disagree with the model,
and you can't disagree with a number you weren't given a reason for.
"""
from __future__ import annotations

from src.models import PositionSizing, RiskDecision, Signal
from src.portfolio import PortfolioState

ARROW = {"long": "▲ LONG", "short": "▼ SHORT"}


def signal_text(
    signal: Signal,
    sizing: PositionSizing,
    decision: RiskDecision,
    *,
    mode: str = "alert",
    paper: bool = True,
) -> str:
    venue = "PAPER" if paper else "*** LIVE ***"
    header = f"{ARROW.get(signal.direction, signal.direction.upper())} {signal.symbol} [{venue}]"

    lines = [
        header,
        "",
        f"Entry   {signal.entry:,.2f}",
        f"Stop    {signal.stop:,.2f}   (-{signal.risk_per_share:,.2f}/share)",
        f"Target  {signal.target:,.2f}   (+{signal.reward_per_share:,.2f}/share)",
        f"R:R     {signal.risk_reward:.2f}",
        "",
        f"Size    {sizing.shares:g} @ risk ${sizing.dollar_risk:,.2f} "
        f"({sizing.account_risk_pct:.2f}% of account)",
        f"Notional ${sizing.notional:,.2f}",
    ]
    if sizing.capped_by:
        lines.append(f"Size limited by: {sizing.capped_by}")

    lines += [
        "",
        f"Confidence {signal.confidence:.0%} | {signal.timeframe} | source: {signal.source}",
        f"News impact: {signal.news_impact}",
        "",
        "WHY:",
        signal.reasoning or "(none given)",
    ]

    if signal.invalidation:
        lines += ["", "INVALIDATED IF:"]
        lines += [f"  - {item}" for item in signal.invalidation]

    if signal.risk_notes:
        lines += ["", "RISKS:", signal.risk_notes]

    if decision.warnings:
        lines += ["", "WARNINGS:"]
        lines += [f"  ! {w}" for w in decision.warnings]

    lines += ["", f"Signal ID: {signal.id}"]
    if mode == "alert":
        lines += ["", "Awaiting your approval. Nothing has been ordered."]
    else:
        lines += ["", "AUTO mode: order submitted."]

    return "\n".join(lines)


def rejection_text(signal: Signal, decision: RiskDecision) -> str:
    return "\n".join(
        [
            f"BLOCKED {signal.symbol} {signal.direction} @ {signal.entry:,.2f}",
            "",
            "Risk gate rejected this setup:",
            *[f"  - {r}" for r in decision.reasons],
            "",
            f"(R:R {signal.risk_reward:.2f}, confidence {signal.confidence:.0%})",
        ]
    )


def halt_text(decision: RiskDecision, state: PortfolioState) -> str:
    return "\n".join(
        [
            "TRADING HALTED",
            "",
            *decision.reasons,
            "",
            f"Equity ${state.account.equity:,.2f} | day {state.day_pnl_pct:+.2f}% | "
            f"drawdown -{state.drawdown_pct:.2f}%",
            f"Open positions: {len(state.positions)}",
        ]
    )


def fill_text(signal: Signal, qty: float, order_id: str, paper: bool) -> str:
    venue = "PAPER" if paper else "*** LIVE ***"
    return "\n".join(
        [
            f"ORDER SUBMITTED [{venue}]",
            f"{signal.symbol} {signal.direction} {qty:g} @ ~{signal.entry:,.2f}",
            f"Stop {signal.stop:,.2f} | Target {signal.target:,.2f}",
            f"Broker order: {order_id or 'n/a'}",
        ]
    )


def discord_embed(signal: Signal, sizing: PositionSizing, decision: RiskDecision, paper: bool) -> dict:
    colour = 0x2ECC71 if signal.direction == "long" else 0xE74C3C
    return {
        "title": f"{ARROW.get(signal.direction, '')} {signal.symbol} "
                 f"{'(paper)' if paper else '(LIVE)'}",
        "color": colour,
        "fields": [
            {"name": "Entry", "value": f"{signal.entry:,.2f}", "inline": True},
            {"name": "Stop", "value": f"{signal.stop:,.2f}", "inline": True},
            {"name": "Target", "value": f"{signal.target:,.2f}", "inline": True},
            {"name": "R:R", "value": f"{signal.risk_reward:.2f}", "inline": True},
            {"name": "Size", "value": f"{sizing.shares:g}", "inline": True},
            {
                "name": "Risk",
                "value": f"${sizing.dollar_risk:,.2f} ({sizing.account_risk_pct:.2f}%)",
                "inline": True,
            },
            {"name": "Reasoning", "value": (signal.reasoning or "-")[:1024]},
            {
                "name": "Invalidated if",
                "value": ("\n".join(f"- {i}" for i in signal.invalidation) or "-")[:1024],
            },
        ],
        "footer": {"text": f"ID {signal.id} | confidence {signal.confidence:.0%}"},
    }
