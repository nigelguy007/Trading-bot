"""
AI journaling: reviews closed trades and looks for recurring mistakes.

Two jobs, matching the guide's Trade Journaling and Strategy Optimization
prompts. Both are told to judge process rather than outcome, because a
strategy review that grades on P&L just rediscovers which trades happened to
win.

The optimizer refuses to run on a thin sample. Reading rule changes out of
eleven trades is how curve-fitting starts.
"""
from __future__ import annotations

import json

from src.ai import prompts
from src.ai.client import LLMClient, LLMError
from src.logging_setup import get_logger
from src.metrics import MIN_MEANINGFUL_TRADES, compute_metrics

log = get_logger(__name__)


class JournalReviewer:
    def __init__(self, llm: LLMClient) -> None:
        self.llm = llm

    def review_trade(self, trade: dict) -> str:
        """The guide's Trade Journaling prompt, on one closed trade."""
        summary = (
            f"{trade.get('symbol')} {trade.get('direction')} "
            f"{trade.get('qty')} shares, entry {trade.get('entry')}, "
            f"stop {trade.get('stop')}, target {trade.get('target')}, "
            f"exit {trade.get('exit')}, P&L {trade.get('pnl'):+.2f}, "
            f"R-multiple {trade.get('r_multiple'):+.2f}, "
            f"exit reason '{trade.get('exit_reason') or 'unknown'}', "
            f"strategy '{trade.get('strategy') or 'unknown'}', "
            f"opened {trade.get('opened_at')}, closed {trade.get('closed_at')}"
        )
        try:
            response = self.llm.complete(
                prompts.JOURNAL_SYSTEM,
                prompts.USER_TRADE_JOURNALING.format(trade=summary),
                effort="medium",
                max_tokens=2048,
            )
        except LLMError as exc:
            log.warning("journal_review.failed", error=str(exc))
            return ""
        return response.text if response.ok else ""

    def optimize_strategy(self, strategy: str, trades: list[dict]) -> str:
        """The guide's Strategy Optimization prompt, over a strategy's history."""
        if len(trades) < MIN_MEANINGFUL_TRADES:
            return (
                f"Only {len(trades)} closed trades for '{strategy}'. That is not enough "
                f"to separate signal from noise — the guide's bar is {MIN_MEANINGFUL_TRADES}+. "
                "Keep paper trading; tuning rules on this sample is curve-fitting."
            )

        metrics = compute_metrics(trades)
        compact = [
            {
                "symbol": t.get("symbol"),
                "direction": t.get("direction"),
                "r": round(float(t.get("r_multiple") or 0), 2),
                "pnl": round(float(t.get("pnl") or 0), 2),
                "exit_reason": t.get("exit_reason"),
                "opened": (t.get("opened_at") or "")[:10],
            }
            for t in trades
        ]

        try:
            response = self.llm.complete(
                prompts.JOURNAL_SYSTEM,
                prompts.USER_STRATEGY_OPTIMIZATION.format(
                    count=len(trades),
                    strategy=strategy,
                    data=json.dumps(compact, indent=2)[:12_000],
                    metrics=metrics.summary(),
                ),
            )
        except LLMError as exc:
            log.warning("journal_review.optimize_failed", error=str(exc))
            return ""

        if not response.ok:
            return ""
        return (
            f"{response.text}\n\n"
            "--- Reminder ---\n"
            "Any rule change suggested above is a hypothesis, not an improvement. "
            "Backtest it on data it was NOT derived from, then forward-test it on "
            "paper before it touches live size. If the backtest looks perfect, "
            "you have curve-fitted it."
        )
