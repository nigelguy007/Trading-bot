"""
Execution — the only component that can spend money.

The behaviour under test is that alert mode really does place nothing, and that
an order appears exactly once, only after a human approves.
"""
from __future__ import annotations

import pytest

from src.approvals import ApprovalStore
from src.broker.base import Broker
from src.broker.registry import BrokerRouter
from src.config import Settings
from src.execution import ExecutionEngine
from src.journal import TradeJournal
from src.models import Account, OrderResult, Position, PositionSizing, RiskDecision, Signal


class RecordingBroker(Broker):
    name = "recording"

    def __init__(self, paper: bool = True, accept: bool = True) -> None:
        self.orders: list[Signal] = []
        self._paper = paper
        self._accept = accept
        self.positions: list[Position] = []

    @property
    def is_paper(self) -> bool:
        return self._paper

    def get_account(self) -> Account:
        return Account(equity=10_000, cash=10_000, buying_power=10_000, last_equity=10_000)

    def get_positions(self) -> list[Position]:
        return self.positions

    def submit_bracket_order(self, signal, sizing) -> OrderResult:
        self.orders.append(signal)
        return OrderResult(
            accepted=self._accept,
            broker=self.name,
            symbol=signal.symbol,
            qty=sizing.shares,
            side=signal.direction,
            broker_order_id="ord-1" if self._accept else "",
            message="" if self._accept else "rejected by broker",
        )

    def close_position(self, symbol: str) -> OrderResult:
        self.positions = [p for p in self.positions if p.symbol != symbol]
        return OrderResult(accepted=True, broker=self.name, symbol=symbol, qty=1, side="close")


class SilentAlerts:
    def __init__(self) -> None:
        self.sent: list[str] = []

    def signal(self, *a, **kw): self.sent.append("signal")
    def rejection(self, *a, **kw): self.sent.append("rejection")
    def halt(self, *a, **kw): self.sent.append("halt")
    def fill(self, *a, **kw): self.sent.append("fill")
    def broadcast(self, *a, **kw): self.sent.append("broadcast")


def make_signal(**kw) -> Signal:
    defaults = dict(
        symbol="AAPL", direction="long", entry=100.0, stop=98.0, target=105.0,
        confidence=0.8, reasoning="breakout", invalidation=["loses 98"],
    )
    defaults.update(kw)
    return Signal(**defaults)


def approved() -> RiskDecision:
    return RiskDecision(approved=True, sizing=PositionSizing(50, 100.0, 2.0, 5000.0, 1.0))


@pytest.fixture
def make_engine(tmp_path):
    def _make(execution_mode: str = "alert", broker: RecordingBroker | None = None):
        settings = Settings(
            anthropic_api_key="test", execution_mode=execution_mode, _env_file=None
        )
        broker = broker or RecordingBroker()
        journal = TradeJournal(tmp_path / f"{execution_mode}.db")
        approvals = ApprovalStore(tmp_path / f"{execution_mode}.json", 45)
        alerts = SilentAlerts()
        engine = ExecutionEngine(
            settings, BrokerRouter(broker), journal, approvals, alerts
        )
        return engine, broker, journal, alerts

    return _make


# --- alert mode --------------------------------------------------------- #
def test_alert_mode_places_no_order(make_engine):
    engine, broker, journal, alerts = make_engine("alert")
    signal = make_signal()

    status = engine.handle_signal(signal, approved())

    assert status == "pending_approval"
    assert broker.orders == []           # nothing was ordered
    assert len(engine.approvals.pending()) == 1
    assert journal.get_signal(signal.id)["status"] == "pending_approval"
    assert "signal" in alerts.sent


def test_approval_places_exactly_one_order(make_engine):
    engine, broker, journal, _ = make_engine("alert")
    signal = make_signal()
    engine.handle_signal(signal, approved())

    ok, message = engine.approve(signal.id)

    assert ok, message
    assert len(broker.orders) == 1
    assert broker.orders[0].symbol == "AAPL"
    assert journal.get_signal(signal.id)["status"] == "submitted"
    assert len(journal.open_trades()) == 1


def test_double_approval_does_not_double_order(make_engine):
    engine, broker, _, _ = make_engine("alert")
    signal = make_signal()
    engine.handle_signal(signal, approved())

    assert engine.approve(signal.id)[0] is True
    assert engine.approve(signal.id)[0] is False
    assert len(broker.orders) == 1


def test_denial_places_no_order(make_engine):
    engine, broker, journal, _ = make_engine("alert")
    signal = make_signal()
    engine.handle_signal(signal, approved())

    ok, _ = engine.deny(signal.id)

    assert ok
    assert broker.orders == []
    assert journal.get_signal(signal.id)["status"] == "denied"


def test_approving_an_unknown_signal_fails_safely(make_engine):
    engine, broker, _, _ = make_engine("alert")
    ok, message = engine.approve("sig_nope")
    assert not ok and broker.orders == []
    assert "unknown" in message


# --- rejection path ------------------------------------------------------ #
def test_risk_rejection_never_reaches_the_broker(make_engine):
    engine, broker, journal, alerts = make_engine("alert")
    signal = make_signal()

    status = engine.handle_signal(signal, RiskDecision(False, ["R:R too low"]))

    assert status == "rejected"
    assert broker.orders == []
    assert engine.approvals.pending() == []
    assert journal.get_signal(signal.id)["status"] == "rejected"
    assert "rejection" in alerts.sent


# --- auto mode ----------------------------------------------------------- #
def test_auto_mode_orders_without_approval(make_engine):
    engine, broker, journal, _ = make_engine("auto")
    signal = make_signal()

    status = engine.handle_signal(signal, approved())

    assert status == "submitted"
    assert len(broker.orders) == 1
    assert engine.approvals.pending() == []
    assert journal.get_signal(signal.id)["status"] == "submitted"


def test_broker_rejection_is_reported_not_swallowed(make_engine):
    engine, _, journal, _ = make_engine("auto", RecordingBroker(accept=False))
    signal = make_signal()

    status = engine.handle_signal(signal, approved())

    assert status == "order_failed"
    assert journal.open_trades() == []


# --- the live-mode backstop ---------------------------------------------- #
def test_a_live_broker_without_confirmation_is_blocked(make_engine):
    """Settings say paper; a broker claiming live must not be ordered through."""
    engine, broker, _, _ = make_engine("auto", RecordingBroker(paper=False))
    signal = make_signal()

    status = engine.handle_signal(signal, approved())

    assert status == "order_failed"
    assert broker.orders == []


# --- expiry --------------------------------------------------------------- #
def test_expired_approvals_are_marked_in_the_journal(make_engine):
    from datetime import datetime, timedelta, timezone

    engine, _, journal, _ = make_engine("alert")
    signal = make_signal()
    engine.handle_signal(signal, approved())

    engine.approvals._data[signal.id]["created_at"] = (
        datetime.now(timezone.utc) - timedelta(hours=5)
    ).isoformat()

    assert engine.expire_stale_approvals() == 1
    assert journal.get_signal(signal.id)["status"] == "expired"
