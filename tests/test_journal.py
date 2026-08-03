"""Trade journal — the record everything else is measured against."""
from __future__ import annotations

import pytest

from src.journal import TradeJournal
from src.models import OrderResult, PositionSizing, RiskDecision, Signal


@pytest.fixture
def journal(tmp_path) -> TradeJournal:
    j = TradeJournal(tmp_path / "journal.db")
    yield j
    j.close()


def make_signal(**kw) -> Signal:
    defaults = dict(
        symbol="AAPL", direction="long", entry=100.0, stop=98.0, target=105.0,
        confidence=0.8, reasoning="breakout", invalidation=["loses 98"],
    )
    defaults.update(kw)
    return Signal(**defaults)


def approved() -> RiskDecision:
    return RiskDecision(
        approved=True,
        sizing=PositionSizing(50, 100.0, 2.0, 5000.0, 1.0),
    )


def test_signal_round_trip(journal):
    signal = make_signal()
    journal.log_signal(signal, approved(), status="pending_approval")

    stored = journal.get_signal(signal.id)
    assert stored["symbol"] == "AAPL"
    assert stored["status"] == "pending_approval"
    assert stored["shares"] == 50
    assert stored["risk_approved"] == 1


def test_rejections_are_recorded_too(journal):
    signal = make_signal()
    journal.log_signal(signal, RiskDecision(False, ["R:R too low"]), status="rejected")

    stored = journal.get_signal(signal.id)
    assert stored["risk_approved"] == 0
    assert "R:R too low" in stored["risk_reason"]


def test_status_updates(journal):
    signal = make_signal()
    journal.log_signal(signal, approved(), status="pending_approval")
    journal.update_signal_status(signal.id, "submitted")
    assert journal.get_signal(signal.id)["status"] == "submitted"


def test_winning_trade_r_multiple(journal):
    """Entry 100, stop 98 -> $2 risk/share. Exit 106 -> +$6/share = +3R."""
    signal = make_signal()
    journal.log_signal(signal, approved(), status="submitted")
    trade_id = journal.open_trade(signal, qty=50)

    closed = journal.close_trade(trade_id, exit_price=106.0, exit_reason="target")
    assert closed["pnl"] == pytest.approx(300.0)
    assert closed["r_multiple"] == pytest.approx(3.0)
    assert closed["status"] == "closed"


def test_losing_trade_at_the_stop_is_minus_one_r(journal):
    signal = make_signal()
    journal.log_signal(signal, approved(), status="submitted")
    trade_id = journal.open_trade(signal, qty=50)

    closed = journal.close_trade(trade_id, exit_price=98.0, exit_reason="stop")
    assert closed["pnl"] == pytest.approx(-100.0)
    assert closed["r_multiple"] == pytest.approx(-1.0)


def test_short_trade_pnl_direction(journal):
    signal = make_signal(direction="short", entry=100.0, stop=102.0, target=94.0)
    journal.log_signal(signal, approved(), status="submitted")
    trade_id = journal.open_trade(signal, qty=50)

    closed = journal.close_trade(trade_id, exit_price=94.0, exit_reason="target")
    assert closed["pnl"] == pytest.approx(300.0)
    assert closed["r_multiple"] == pytest.approx(3.0)


def test_fees_reduce_pnl(journal):
    signal = make_signal()
    journal.log_signal(signal, approved(), status="submitted")
    trade_id = journal.open_trade(signal, qty=50)

    closed = journal.close_trade(trade_id, exit_price=106.0, fees=10.0)
    assert closed["pnl"] == pytest.approx(290.0)


def test_open_and_closed_trade_lists(journal):
    signal = make_signal()
    journal.log_signal(signal, approved(), status="submitted")
    trade_id = journal.open_trade(signal, qty=50)

    assert len(journal.open_trades()) == 1
    assert journal.closed_trades() == []

    journal.close_trade(trade_id, 106.0)
    assert journal.open_trades() == []
    assert len(journal.closed_trades()) == 1


def test_open_stops_feed_risk_accounting(journal):
    signal = make_signal()
    journal.log_signal(signal, approved(), status="submitted")
    journal.open_trade(signal, qty=50)
    assert journal.open_stops() == {"AAPL": 98.0}


def test_closing_an_unknown_trade_is_safe(journal):
    assert journal.close_trade("trd_nope", 100.0) is None


def test_orders_are_logged(journal):
    signal = make_signal()
    journal.log_signal(signal, approved(), status="submitted")
    journal.log_order(
        OrderResult(
            accepted=True, broker="alpaca", symbol="AAPL", qty=50,
            side="buy", broker_order_id="abc-123", status="accepted",
        ),
        signal.id,
    )
    row = journal.conn.execute("SELECT * FROM orders").fetchone()
    assert row["broker_order_id"] == "abc-123"
    assert row["signal_id"] == signal.id


def test_equity_curve_and_export(journal, tmp_path):
    journal.record_equity(10_000.0, 5_000.0, 5_000.0)
    assert len(journal.equity_curve()) == 1

    signal = make_signal()
    journal.log_signal(signal, approved(), status="submitted")
    trade_id = journal.open_trade(signal, qty=50)
    journal.close_trade(trade_id, 106.0)

    path = journal.export_csv(tmp_path / "out.csv")
    contents = path.read_text(encoding="utf-8")
    assert "AAPL" in contents
    assert "r_multiple" in contents


def test_run_bookkeeping(journal):
    run_id = journal.start_run()
    journal.finish_run(run_id, scanned=8, signals=2, approved=1, errors=0, notes="ok")
    row = journal.conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    assert row["scanned"] == 8 and row["notes"] == "ok"
