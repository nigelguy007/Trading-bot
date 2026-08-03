"""
Execution: the last step, and the only one that can spend money.

Two modes:

  alert (default) — the signal is queued for approval and an alert goes out.
                    Nothing is ordered. You are the trigger.
  auto            — the order goes straight to the broker.

`auto` is deliberately awkward to reach: it needs EXECUTION_MODE=auto, and if
it is also pointed at a live account it needs the full three-switch live
confirmation as well. Full auto-execution against real money is an advanced
mode, not a default.
"""
from __future__ import annotations

from typing import Optional

from src.alerts.router import AlertRouter
from src.approvals import ApprovalStore, signal_from_entry
from src.broker.base import Broker
from src.broker.registry import BrokerRouter
from src.config import Settings
from src.journal import TradeJournal
from src.logging_setup import get_logger
from src.models import OrderResult, PositionSizing, RiskDecision, Signal

log = get_logger(__name__)


class ExecutionEngine:
    def __init__(
        self,
        settings: Settings,
        brokers: BrokerRouter,
        journal: TradeJournal,
        approvals: ApprovalStore,
        alerts: AlertRouter,
    ) -> None:
        self.settings = settings
        self.brokers = brokers
        self.journal = journal
        self.approvals = approvals
        self.alerts = alerts

    # ------------------------------------------------------------------ #
    @property
    def auto_mode(self) -> bool:
        return self.settings.execution_mode == "auto"

    def _broker_for(self, signal: Signal) -> Broker:
        return self.brokers.for_asset_class(signal.asset_class)

    # ------------------------------------------------------------------ #
    def handle_signal(self, signal: Signal, decision: RiskDecision) -> str:
        """
        Route an approved signal. Returns the status recorded in the journal.
        """
        if not decision.approved or decision.sizing is None:
            self.journal.log_signal(signal, decision, status="rejected")
            self.alerts.rejection(signal, decision)
            return "rejected"

        broker = self._broker_for(signal)

        if self.auto_mode:
            self.journal.log_signal(signal, decision, status="auto_submitting")
            result = self._place(signal, decision.sizing, broker)
            status = "submitted" if result.accepted else "order_failed"
            self.journal.update_signal_status(signal.id, status)
            self.alerts.signal(
                signal, decision.sizing, decision, paper=broker.is_paper, actionable=False
            )
            if result.accepted:
                self.alerts.fill(
                    signal, decision.sizing.shares, result.broker_order_id, broker.is_paper
                )
            return status

        # Alert mode: draft the trade, hand it to the human, order nothing.
        self.approvals.add(signal, decision.sizing)
        self.journal.log_signal(signal, decision, status="pending_approval")
        self.alerts.signal(
            signal, decision.sizing, decision, paper=broker.is_paper, actionable=True
        )
        log.info("execution.awaiting_approval", signal_id=signal.id, symbol=signal.symbol)
        return "pending_approval"

    # ------------------------------------------------------------------ #
    def approve(self, signal_id: str) -> tuple[bool, str]:
        """Approve a pending signal and place the order. (ok, message)."""
        entry = self.approvals.resolve(signal_id, "approved")
        if entry is None:
            return False, (
                "That signal is unknown, already resolved, or expired. "
                "Expired setups are not re-priced — wait for a fresh one."
            )

        signal, sizing = signal_from_entry(entry)
        broker = self._broker_for(signal)
        result = self._place(signal, sizing, broker)

        if not result.accepted:
            self.journal.update_signal_status(signal.id, "order_failed")
            return False, f"Broker rejected the order: {result.message}"

        self.journal.update_signal_status(signal.id, "submitted")
        self.alerts.fill(signal, sizing.shares, result.broker_order_id, broker.is_paper)
        venue = "paper" if broker.is_paper else "LIVE"
        return True, f"Submitted {signal.symbol} {signal.direction} {sizing.shares:g} ({venue})."

    def deny(self, signal_id: str) -> tuple[bool, str]:
        entry = self.approvals.resolve(signal_id, "denied")
        if entry is None:
            return False, "That signal is unknown, already resolved, or expired."
        self.journal.update_signal_status(signal_id, "denied")
        return True, "Denied. Nothing was ordered."

    def expire_stale_approvals(self) -> int:
        expired = self.approvals.expire_stale()
        for signal_id in expired:
            self.journal.update_signal_status(signal_id, "expired")
        return len(expired)

    # ------------------------------------------------------------------ #
    def _place(self, signal: Signal, sizing: PositionSizing, broker: Broker) -> OrderResult:
        if not broker.is_paper and not self.settings.live_trading_enabled:
            # Belt and braces: should be unreachable, but this is the one place
            # where being wrong costs real money.
            message = "Live order blocked: the live-trading confirmation is not set."
            log.error("execution.live_blocked", symbol=signal.symbol)
            return OrderResult(
                accepted=False, broker=broker.name, symbol=signal.symbol,
                qty=sizing.shares, side=signal.direction, message=message,
            )

        result = broker.submit_bracket_order(signal, sizing)
        self.journal.log_order(result, signal.id)
        if result.accepted:
            self.journal.open_trade(signal, sizing.shares, strategy=signal.source)
        return result

    # ------------------------------------------------------------------ #
    def close_position(self, symbol: str, reason: str = "manual") -> tuple[bool, str]:
        for broker in self.brokers.all():
            # Read the mark BEFORE closing — afterwards the position is gone
            # and there is nothing left to price the journal entry against.
            position = next((p for p in broker.get_positions() if p.symbol == symbol), None)
            if position is None:
                continue

            result = broker.close_position(symbol)
            if not result.accepted:
                return False, result.message

            exit_price = position.market_price or position.avg_entry
            for trade in self.journal.open_trades():
                if trade["symbol"] == symbol:
                    self.journal.close_trade(trade["id"], exit_price, exit_reason=reason)
            return True, f"Closed {symbol} at {exit_price:,.2f}."
        return False, f"No open position in {symbol}."
