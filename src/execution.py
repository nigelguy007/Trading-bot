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

from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

from src.alerts.router import AlertRouter
from src.approvals import ApprovalStore, signal_from_entry
from src.broker.base import Broker
from src.broker.registry import BrokerRouter
from src.config import Settings
from src.journal import TradeJournal
from src.killswitch import KillSwitch
from src.logging_setup import get_logger
from src.models import OrderResult, PositionSizing, RiskDecision, Signal

log = get_logger(__name__)

# A journal trade younger than this is not declared closed just because the
# broker shows no position yet — the entry may still be working.
CLOSE_DETECTION_GRACE = timedelta(minutes=30)


class ExecutionEngine:
    def __init__(
        self,
        settings: Settings,
        brokers: BrokerRouter,
        journal: TradeJournal,
        approvals: ApprovalStore,
        alerts: AlertRouter,
        kill_switch: KillSwitch | None = None,
    ) -> None:
        self.settings = settings
        self.brokers = brokers
        self.journal = journal
        self.approvals = approvals
        self.alerts = alerts
        self.kill_switch = kill_switch or KillSwitch(settings.data_path)

    # ------------------------------------------------------------------ #
    @property
    def auto_mode(self) -> bool:
        return self.settings.execution_mode == "auto"

    def _broker_for(self, signal: Signal) -> Broker:
        # By symbol, not just asset class: IG and Trading 212 are both
        # "equity", and which one takes a signal is the entire point of
        # running them together.
        return self.brokers.for_signal(signal)

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
                    signal,
                    decision.sizing.shares,
                    result.broker_order_id,
                    broker.is_paper,
                    decision.sizing.unit,
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
        self.alerts.fill(
            signal, sizing.shares, result.broker_order_id, broker.is_paper, sizing.unit
        )
        venue = "paper" if broker.is_paper else "LIVE"
        return True, (
            f"Submitted {signal.symbol} {signal.direction} {sizing.size_label} ({venue})."
        )

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
        # The kill switch is checked HERE, at the last line before money moves,
        # not only at the top of a cycle — it may have been pulled mid-cycle,
        # or between an alert and your tap on "approve".
        kill = self.kill_switch.state()
        if kill is not None:
            log.warning("execution.blocked_by_kill_switch", symbol=signal.symbol)
            return OrderResult(
                accepted=False, broker=broker.name, symbol=signal.symbol,
                qty=sizing.shares, side=signal.direction,
                message=f"Order blocked: {kill.describe()}",
            )

        if not broker.is_paper and not self.settings.live_enabled_for(broker.name):
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
    def reconcile(self) -> dict[str, dict]:
        """
        Let each broker repair whatever its API cannot do atomically.

        Only Trading 212 needs this today — it has no OCO, so a filled target
        leaves its stop order resting against shares that are gone. Called on
        startup and every scheduler cycle.
        """
        results: dict[str, dict] = {}
        for broker in self.brokers.all():
            fixer = getattr(broker, "reconcile", None)
            if not callable(fixer):
                continue
            try:
                results[broker.name] = fixer()
            except Exception as exc:  # pragma: no cover - never fatal
                log.warning("execution.reconcile_failed", venue=broker.name, error=str(exc))
        return results

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

    # ------------------------------------------------------------------ #
    def engage_kill_switch(
        self, reason: str = "manual", source: str = "unknown", flatten: bool = False
    ) -> str:
        """
        Stop all trading now. Optionally market-close every open position.

        The switch is engaged BEFORE flattening, so nothing can open a new
        position while the old ones are being closed.
        """
        state = self.kill_switch.engage(reason, source)
        lines = [f"KILL SWITCH ENGAGED — {state.reason} (via {state.source})",
                 "No new orders will be placed."]
        if flatten:
            lines += self.flatten_all(reason="kill switch")
        else:
            lines.append("Open positions keep their broker-side stops and targets.")
        lines.append("Resume only from the CLI: python -m src.cli resume --yes")
        message = "\n".join(lines)
        self.alerts.broadcast(message)
        return message

    def flatten_all(self, reason: str = "flatten") -> list[str]:
        """Market-close every position on every broker. One line per outcome."""
        out: list[str] = []
        for broker in self.brokers.all():
            try:
                positions = broker.get_positions()
            except Exception as exc:
                out.append(f"{broker.name}: could not read positions ({exc}) — CLOSE MANUALLY")
                continue
            for position in positions:
                ok, msg = self.close_position(position.symbol, reason=reason)
                out.append(msg if ok else f"FAILED {position.symbol}: {msg} — CLOSE MANUALLY")
        return out or ["No open positions to close."]

    def sync_closed_trades(
        self, price_of: Optional[Callable[[str], float]] = None
    ) -> list[dict]:
        """
        Find journal trades the broker has already closed — a bracket's stop or
        take-profit filled — record them, and alert.

        Without this the journal says "open" forever and nobody hears about the
        take-profit. Broker order-fill APIs differ per venue, so the exit price
        is ESTIMATED: whichever of stop/target is nearer the current price, and
        labelled "(est.)" everywhere it appears. Check the broker for the fill.
        """
        try:
            held = {p.symbol for b in self.brokers.all() for p in b.get_positions()}
        except Exception as exc:
            # If we cannot see positions we must not conclude they are gone.
            log.warning("execution.sync_skipped", error=str(exc))
            return []

        now = datetime.now(timezone.utc)
        closed: list[dict] = []
        for trade in self.journal.open_trades():
            if trade["symbol"] in held or not _older_than(trade.get("opened_at"), now):
                continue
            leg, exit_price = _likely_leg(trade, price_of)
            row = self.journal.close_trade(trade["id"], exit_price, exit_reason=f"{leg} (est.)")
            if row is None:
                continue
            closed.append(row)
            verb = "TAKE-PROFIT HIT" if leg == "target" else "STOPPED OUT"
            self.alerts.broadcast(
                f"{verb} — {trade['symbol']} {trade['direction']} closed at the broker. "
                f"Est. exit {exit_price:,.2f}, {row['r_multiple']:+.2f}R "
                f"(estimate — confirm the fill on the broker)."
            )
        return closed


def _older_than(opened_at: Optional[str], now: datetime) -> bool:
    if not opened_at:
        return True
    try:
        ts = datetime.fromisoformat(opened_at.replace("Z", "+00:00"))
    except ValueError:
        return True
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return now - ts >= CLOSE_DETECTION_GRACE


def _likely_leg(trade: dict, price_of: Optional[Callable[[str], float]]) -> tuple[str, float]:
    stop, target = float(trade["stop"]), float(trade["target"])
    price = None
    if price_of is not None:
        try:
            price = float(price_of(trade["symbol"]))
        except Exception:
            price = None
    if price is None:
        # No price: assume the stop. Overstating a loss is the safe mistake.
        return "stop", stop
    return ("target", target) if abs(price - target) < abs(price - stop) else ("stop", stop)
