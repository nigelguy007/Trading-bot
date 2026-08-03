"""The human-in-the-loop gate."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.approvals import ApprovalStore, signal_from_entry
from src.models import PositionSizing, Signal


@pytest.fixture
def store(tmp_path) -> ApprovalStore:
    return ApprovalStore(tmp_path / "approvals.json", ttl_minutes=45)


def make_signal(**kw) -> Signal:
    defaults = dict(
        symbol="AAPL", direction="long", entry=100.0, stop=98.0, target=105.0,
        confidence=0.8, reasoning="breakout", invalidation=["loses 98"],
    )
    defaults.update(kw)
    return Signal(**defaults)


def sizing() -> PositionSizing:
    return PositionSizing(50, 100.0, 2.0, 5000.0, 1.0)


def test_added_signal_is_pending(store):
    signal = make_signal()
    store.add(signal, sizing())

    pending = store.pending()
    assert len(pending) == 1
    assert pending[0]["signal"]["symbol"] == "AAPL"


def test_approve_then_the_signal_is_no_longer_pending(store):
    signal = make_signal()
    store.add(signal, sizing())

    assert store.resolve(signal.id, "approved") is not None
    assert store.pending() == []


def test_a_signal_cannot_be_approved_twice(store):
    """Double-tapping Approve on a phone must not place two orders."""
    signal = make_signal()
    store.add(signal, sizing())

    assert store.resolve(signal.id, "approved") is not None
    assert store.resolve(signal.id, "approved") is None


def test_unknown_signal_resolves_to_nothing(store):
    assert store.resolve("sig_nope", "approved") is None


def test_stale_approvals_expire_rather_than_filling(store):
    signal = make_signal()
    store.add(signal, sizing())

    # Backdate it past the TTL — the setup is no longer the one you looked at.
    store._data[signal.id]["created_at"] = (
        datetime.now(timezone.utc) - timedelta(hours=3)
    ).isoformat()

    assert store.resolve(signal.id, "approved") is None
    assert store._data[signal.id]["status"] == "expired"


def test_expire_stale_reports_what_it_expired(store):
    signal = make_signal()
    store.add(signal, sizing())
    store._data[signal.id]["created_at"] = (
        datetime.now(timezone.utc) - timedelta(hours=3)
    ).isoformat()

    assert store.expire_stale() == [signal.id]


def test_state_survives_a_restart(store, tmp_path):
    signal = make_signal()
    store.add(signal, sizing())

    reloaded = ApprovalStore(tmp_path / "approvals.json", ttl_minutes=45)
    assert len(reloaded.pending()) == 1


def test_signal_round_trips_through_storage(store):
    original = make_signal()
    store.add(original, sizing())

    restored, restored_sizing = signal_from_entry(store.get(original.id))
    assert restored.id == original.id
    assert restored.symbol == original.symbol
    assert restored.entry == original.entry
    assert restored.risk_reward == pytest.approx(original.risk_reward)
    assert restored_sizing.shares == 50
