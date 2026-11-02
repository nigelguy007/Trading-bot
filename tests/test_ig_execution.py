"""
The full gate-to-IG path, with IG mocked at the wire.

Unit tests prove each piece. This proves the pieces are connected: that alert
mode really does leave IG untouched, that approving sends exactly one deal with
the stake the risk gate computed, and that a live deal with the confirmation
missing is blocked even though the adapter itself would happily send it.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.alerts.router import AlertRouter
from src.approvals import ApprovalStore
from src.broker.registry import BrokerRouter
from src.execution import ExecutionEngine
from src.journal import TradeJournal
from src.models import RiskDecision
from src.portfolio import PortfolioManager
from src.risk import RiskManager
from src.sizing import calculate_size

from tests.test_ig_broker import FakeIG, a_signal, build, make_settings

ACCOUNT = {
    "accounts": [
        {
            "accountId": "ABC12",
            "currency": "GBP",
            "balance": {"balance": 10_000.0, "profitLoss": 0.0, "available": 9_000.0},
        }
    ]
}


@pytest.fixture
def rig(tmp_path: Path):
    """A full execution stack pointed at a fake IG."""
    fake = FakeIG(
        {
            "GET /accounts": ACCOUNT,
            "GET /positions": {"positions": []},
            "POST /positions/otc": {"dealReference": "REF1"},
            "GET /confirms/REF1": {
                "dealStatus": "ACCEPTED", "dealId": "D1", "level": 8000.0
            },
        }
    )
    settings = make_settings(
        data_dir=str(tmp_path),
        log_dir=str(tmp_path / "logs"),
        ig_epics="FTSE=IX.D.FTSE.DAILY.IP",
        watchlist="FTSE",
    )
    broker = build(fake, settings)
    journal = TradeJournal(tmp_path / "journal.db")
    engine = ExecutionEngine(
        settings,
        BrokerRouter(broker),
        journal,
        ApprovalStore(tmp_path / "approvals.json"),
        AlertRouter(settings),
    )
    portfolio = PortfolioManager(broker, settings, journal)
    risk = RiskManager(settings, portfolio)
    yield fake, settings, broker, engine, risk, portfolio
    journal.close()


def deals(fake: FakeIG) -> list[dict]:
    return [
        json.loads(c.content)
        for c in fake.calls
        if c.method == "POST" and c.url.path.endswith("/positions/otc")
    ]


# ---------------------------------------------------------------------- #
def test_alert_mode_sends_nothing_to_ig(rig):
    fake, _, _, engine, risk, portfolio = rig
    signal = a_signal(entry=8000, stop=7960, target=8100, confidence=0.9)
    decision = risk.evaluate(signal, portfolio.snapshot())

    assert decision.approved, decision.reasons
    assert engine.handle_signal(signal, decision) == "pending_approval"
    assert deals(fake) == []


def test_approving_sends_exactly_the_stake_the_gate_computed(rig):
    fake, settings, _, engine, risk, portfolio = rig
    signal = a_signal(entry=8000, stop=7960, target=8100, confidence=0.9)
    decision = risk.evaluate(signal, portfolio.snapshot())
    engine.handle_signal(signal, decision)

    ok, message = engine.approve(signal.id)

    assert ok, message
    assert "2.5 GBP/point" in message
    sent = deals(fake)
    assert len(sent) == 1
    assert sent[0]["size"] == 2.5
    assert sent[0]["stopLevel"] == 7960.0
    assert sent[0]["limitLevel"] == 8100.0


def test_the_gate_sizes_against_real_ig_equity(rig):
    """
    The £10,000 comes from IG's /accounts, not from a constant: 1% of it over a
    40-point stop is £2.50 a point.
    """
    _, settings, _, _, _, portfolio = rig
    state = portfolio.snapshot()
    assert state.account.equity == 10_000.0

    sizing = calculate_size(
        a_signal(entry=8000, stop=7960, target=8100), state.account.equity, settings
    )
    assert sizing.shares == pytest.approx(2.5)
    assert sizing.unit == "GBP/point"


def test_a_double_tap_cannot_place_two_geared_bets(rig):
    fake, _, _, engine, risk, portfolio = rig
    signal = a_signal(entry=8000, stop=7960, target=8100, confidence=0.9)
    engine.handle_signal(signal, risk.evaluate(signal, portfolio.snapshot()))

    first_ok, _ = engine.approve(signal.id)
    second_ok, message = engine.approve(signal.id)

    assert first_ok
    assert not second_ok
    assert len(deals(fake)) == 1


def test_a_rejected_signal_never_reaches_ig(rig):
    fake, _, _, engine, _, _ = rig
    signal = a_signal()
    engine.handle_signal(signal, RiskDecision(approved=False, reasons=["R:R too low"]))
    assert deals(fake) == []


def test_a_live_deal_without_the_confirmation_phrase_is_blocked(rig, tmp_path):
    """
    IG_DEMO=false makes the adapter non-paper, but CONFIRM_LIVE is missing.
    The execution engine must refuse before the adapter ever builds a request.
    """
    fake = FakeIG(
        {
            "GET /accounts": ACCOUNT,
            "GET /positions": {"positions": []},
            "POST /positions/otc": {"dealReference": "REF1"},
        }
    )
    settings = make_settings(
        data_dir=str(tmp_path),
        log_dir=str(tmp_path / "logs"),
        ig_epics="FTSE=IX.D.FTSE.DAILY.IP",
        ig_demo=False,
        trading_mode="live",
        confirm_live="",  # the switch that is missing
    )
    broker = build(fake, settings)
    journal = TradeJournal(tmp_path / "live.db")
    engine = ExecutionEngine(
        settings,
        BrokerRouter(broker),
        journal,
        ApprovalStore(tmp_path / "live-approvals.json"),
        AlertRouter(settings),
    )

    assert not broker.is_paper
    assert not settings.live_enabled_for("ig")

    signal = a_signal(entry=8000, stop=7960, target=8100, confidence=0.9)
    sizing = calculate_size(signal, 10_000.0, settings)
    result = engine._place(signal, sizing, broker)

    assert not result.accepted
    assert "confirmation is not set" in result.message
    assert deals(fake) == []
    journal.close()


def test_crypto_is_refused_rather_than_mis_routed(rig):
    from src.broker.base import BrokerError

    _, _, _, engine, _, _ = rig
    signal = a_signal(symbol="BTC/USD", asset_class="crypto")
    with pytest.raises(BrokerError, match="no broker configured for asset class"):
        engine._broker_for(signal)
