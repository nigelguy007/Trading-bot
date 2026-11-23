"""
Kill switch — the one control that has to work when everything else is wrong.

What matters: it blocks at the last line before money moves (not just at the
top of a cycle), it survives restarts, it fails closed, any channel can engage
it, and nothing reachable over the network can release it.

Also here: bracket exits filled at the broker are noticed, journaled and
alerted ("take-profit hit") instead of sitting "open" forever.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from src.approvals import ApprovalStore
from src.broker.registry import BrokerRouter
from src.config import Settings
from src.execution import ExecutionEngine
from src.journal import TradeJournal
from src.killswitch import KillSwitch
from src.models import Position
from tests.test_execution import RecordingBroker, SilentAlerts, approved, make_signal


@pytest.fixture
def switch(tmp_path) -> KillSwitch:
    return KillSwitch(tmp_path)


@pytest.fixture
def engine_for(tmp_path, switch):
    def _make(mode: str = "auto", broker: RecordingBroker | None = None):
        settings = Settings(anthropic_api_key="test", execution_mode=mode, _env_file=None)
        broker = broker or RecordingBroker()
        journal = TradeJournal(tmp_path / f"{mode}.db")
        approvals = ApprovalStore(tmp_path / f"{mode}.json", 45)
        alerts = SilentAlerts()
        engine = ExecutionEngine(
            settings, BrokerRouter(broker), journal, approvals, alerts, kill_switch=switch
        )
        return engine, broker, journal, alerts

    return _make


def _position(symbol: str, qty: float = 10, price: float = 100) -> Position:
    return Position(symbol=symbol, qty=qty, avg_entry=price, market_price=price, unrealized_pnl=0)


# --- the switch itself ------------------------------------------------- #
def test_not_engaged_by_default(switch):
    assert switch.state() is None
    assert not switch.engaged


def test_engage_persists_across_instances(tmp_path, switch):
    switch.engage("testing", source="unit")
    # A fresh instance = a restarted process. It must still be stopped.
    again = KillSwitch(tmp_path)
    assert again.engaged
    assert again.state().reason == "testing"
    assert again.state().source == "unit"


def test_engage_is_idempotent_and_keeps_the_first_reason(switch):
    switch.engage("first", source="a")
    switch.engage("second", source="b")
    assert switch.state().reason == "first"


def test_release(switch):
    switch.engage("x")
    assert switch.release() is True
    assert not switch.engaged
    assert switch.release() is False


def test_a_corrupt_file_fails_closed(switch):
    switch.path.write_text("{not json", encoding="utf-8")
    state = switch.state()
    assert state is not None
    assert state.source == "fail-closed"


# --- execution: the last line before money ----------------------------- #
def test_auto_mode_places_nothing_when_engaged(engine_for, switch):
    engine, broker, journal, _ = engine_for("auto")
    switch.engage("stop", source="unit")

    status = engine.handle_signal(make_signal(), approved())

    assert broker.orders == []
    assert status == "order_failed"
    assert journal.open_trades() == []


def test_approving_after_the_switch_is_pulled_places_nothing(engine_for, switch):
    """The alert went out, THEN you hit kill, THEN someone taps approve."""
    engine, broker, _, _ = engine_for("alert")
    signal = make_signal()
    engine.handle_signal(signal, approved())
    switch.engage("changed my mind", source="unit")

    ok, message = engine.approve(signal.id)

    assert not ok
    assert broker.orders == []
    assert "kill switch" in message


def test_releasing_allows_orders_again(engine_for, switch):
    engine, broker, _, _ = engine_for("auto")
    switch.engage("x")
    switch.release()
    engine.handle_signal(make_signal(), approved())
    assert len(broker.orders) == 1


def test_engage_with_flatten_closes_every_position(engine_for, switch):
    broker = RecordingBroker()
    broker.positions = [_position("AAPL"), _position("MSFT", price=300)]
    engine, broker, _, alerts = engine_for("auto", broker)

    message = engine.engage_kill_switch("panic", source="unit", flatten=True)

    assert switch.engaged
    assert broker.positions == []
    assert "Closed AAPL" in message and "Closed MSFT" in message
    assert "broadcast" in alerts.sent


def test_engage_without_flatten_leaves_positions_alone(engine_for, switch):
    broker = RecordingBroker()
    broker.positions = [_position("AAPL")]
    engine, broker, _, _ = engine_for("auto", broker)

    engine.engage_kill_switch("pause", source="unit")

    assert switch.engaged
    assert len(broker.positions) == 1


# --- bracket exits: the "TP hit" alert --------------------------------- #
def _age(journal: TradeJournal, minutes: int) -> None:
    then = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()
    journal.conn.execute("UPDATE trades SET opened_at = ?", (then,))
    journal.conn.commit()


def test_a_take_profit_filled_at_the_broker_is_journaled_and_alerted(engine_for):
    engine, broker, journal, alerts = engine_for("auto")
    engine.handle_signal(make_signal(entry=100, stop=98, target=105), approved())
    _age(journal, 60)
    broker.positions = []  # the bracket's target filled at the broker

    closed = engine.sync_closed_trades(price_of=lambda s: 105.2)

    assert len(closed) == 1
    assert closed[0]["exit_reason"] == "target (est.)"
    assert closed[0]["exit"] == 105
    assert journal.open_trades() == []
    assert "broadcast" in alerts.sent


def test_a_stop_hit_is_labelled_a_stop(engine_for):
    engine, _, journal, _ = engine_for("auto")
    engine.handle_signal(make_signal(entry=100, stop=98, target=105), approved())
    _age(journal, 60)

    closed = engine.sync_closed_trades(price_of=lambda s: 97.5)

    assert closed[0]["exit_reason"] == "stop (est.)"


def test_without_a_price_the_safe_assumption_is_the_stop(engine_for):
    engine, _, journal, _ = engine_for("auto")
    engine.handle_signal(make_signal(entry=100, stop=98, target=105), approved())
    _age(journal, 60)

    closed = engine.sync_closed_trades(price_of=None)

    assert closed[0]["exit_reason"] == "stop (est.)"


def test_a_fresh_order_is_not_mistaken_for_a_closed_trade(engine_for):
    """A market order that has not filled yet shows no position. Not closed."""
    engine, _, journal, _ = engine_for("auto")
    engine.handle_signal(make_signal(), approved())

    assert engine.sync_closed_trades(price_of=lambda s: 105) == []
    assert len(journal.open_trades()) == 1


def test_a_still_held_position_is_not_closed(engine_for):
    broker = RecordingBroker()
    engine, broker, journal, _ = engine_for("auto", broker)
    engine.handle_signal(make_signal(), approved())
    _age(journal, 60)
    broker.positions = [_position("AAPL", qty=50)]

    assert engine.sync_closed_trades(price_of=lambda s: 102) == []


def test_unreadable_positions_close_nothing(engine_for):
    """If the broker cannot be read, 'no positions' is unknown, not true."""

    class Broken(RecordingBroker):
        def get_positions(self):
            raise RuntimeError("timeout")

    engine, _, journal, _ = engine_for("auto", Broken())
    engine.handle_signal(make_signal(), approved())
    _age(journal, 60)

    assert engine.sync_closed_trades(price_of=lambda s: 105) == []
    assert len(journal.open_trades()) == 1


# --- pipeline + HTTP + Telegram ---------------------------------------- #
@pytest.fixture
def ctx(tmp_path):
    from src.app import build_app

    settings = Settings(
        _env_file=None, anthropic_api_key="", data_dir=str(tmp_path),
        log_dir=str(tmp_path / "logs"), telegram_chat_id="12345",
    )
    context = build_app(settings, force_simulated=True, require_llm=False)
    yield context
    context.close()


@pytest.fixture
def client(ctx, monkeypatch):
    import src.webhook_server as ws

    monkeypatch.setattr(ws, "_ctx", ctx, raising=False)
    monkeypatch.setattr(ws, "get_context", lambda: ctx)
    with TestClient(ws.app) as c:
        yield c


def test_the_pipeline_halts_before_scanning(ctx):
    ctx.execution.kill_switch.engage("stop", source="unit")
    result = ctx.pipeline.run_cycle()
    assert result.halted
    assert "kill switch" in result.halt_reason
    assert result.scanned == 0


def test_the_dashboard_button_engages(client):
    assert client.get("/api/kill").json()["engaged"] is False
    r = client.post("/api/kill", json={"reason": "button"})
    assert r.status_code == 200
    status = client.get("/api/kill").json()
    assert status["engaged"] is True
    assert status["source"] == "dashboard"


def test_there_is_no_http_route_that_releases_it(client, ctx):
    ctx.execution.kill_switch.engage("x", source="unit")
    for method, path in [("delete", "/api/kill"), ("post", "/api/resume"), ("post", "/api/kill/release")]:
        getattr(client, method)(path)
    assert ctx.execution.kill_switch.engaged


def test_the_dashboard_page_has_the_button(client):
    assert 'id="kill-btn"' in client.get("/").text


def test_telegram_kill_from_the_configured_chat(client, ctx):
    r = client.post("/webhook/telegram", json={"message": {"text": "/kill", "chat": {"id": 12345}}})
    assert r.status_code == 200
    assert ctx.execution.kill_switch.state().source == "telegram"


def test_telegram_kill_from_a_stranger_is_refused(client, ctx):
    r = client.post("/webhook/telegram", json={"message": {"text": "/kill", "chat": {"id": 999}}})
    assert r.status_code == 403
    assert not ctx.execution.kill_switch.engaged


def test_other_telegram_text_is_ignored(client, ctx):
    r = client.post("/webhook/telegram", json={"message": {"text": "hello", "chat": {"id": 12345}}})
    assert r.json()["status"] == "ignored"
    assert not ctx.execution.kill_switch.engaged


# --- CLI ---------------------------------------------------------------- #
def test_cli_kill_and_resume(tmp_path, monkeypatch, capsys):
    import src.cli as cli

    settings = Settings(_env_file=None, anthropic_api_key="", data_dir=str(tmp_path),
                        log_dir=str(tmp_path / "logs"))
    monkeypatch.setattr(cli, "get_settings", lambda: settings)

    assert cli.main(["kill", "--reason", "cli test", "--simulated", "--flatten"]) == 0
    assert KillSwitch(settings.data_path).engaged
    out = capsys.readouterr().out
    assert "Could not start the app" not in out  # works with no Claude key
    assert "No open positions to close." in out
    assert cli.main(["kill-status"]) == 2
    assert cli.main(["resume", "--yes"]) == 0
    assert not KillSwitch(settings.data_path).engaged
    assert cli.main(["kill-status"]) == 0
