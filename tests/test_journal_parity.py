"""
SQLite and Postgres must be interchangeable.

This is the test the whole migration rests on. It drives both journals
through the same sequence of real operations and compares every read. A
difference here is a difference in the numbers the bot trades on — the
equity curve, the day's opening equity that the daily-loss breaker
measures against, and realised P&L.

It writes nothing to Supabase. Point TEST_PG_DSN at a throwaway database
(the suite skips without one), never at the live journal.

Writing the port from memory of the SQLite code produced four real bugs
before this test existed, the worst being a close_trade that took P&L as
an argument instead of computing it. Hence comparing behaviour rather
than reading the two files side by side.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone

import pytest

from src.journal import TradeJournal
from src.models import PositionSizing, RiskDecision, Signal

PG_DSN = os.environ.get("TEST_PG_DSN")
pytestmark = pytest.mark.skipif(
    not PG_DSN, reason="set TEST_PG_DSN to a throwaway Postgres to run parity tests"
)

# Written independently by each backend, so they can never match.
# Everything else must.
GENERATED = {"created_at", "opened_at", "closed_at", "ts", "started_at", "finished_at"}


def make_signal(**kw) -> Signal:
    defaults = dict(
        symbol="AAPL", direction="long", entry=100.0, stop=98.0, target=105.0,
        confidence=0.8, reasoning="breakout", invalidation=["loses 98"],
    )
    defaults.update(kw)
    return Signal(**defaults)


def approved(shares: float = 50) -> RiskDecision:
    return RiskDecision(
        approved=True,
        sizing=PositionSizing(shares, 100.0, 2.0, 5000.0, 1.0),
    )


def scrub(value):
    """Drop the fields each backend generates on its own clock."""
    if isinstance(value, dict):
        return {k: scrub(v) for k, v in value.items() if k not in GENERATED}
    if isinstance(value, list):
        return [scrub(v) for v in value]
    return value


@pytest.fixture
def pair(tmp_path):
    """One SQLite journal and one Postgres journal, both empty."""
    import psycopg

    from src.journal_pg import SCHEMA_NAME, PostgresJournal

    # A clean schema per test: parity means nothing if rows leak between runs.
    with psycopg.connect(PG_DSN, autocommit=True) as conn:
        conn.execute(f"DROP SCHEMA IF EXISTS {SCHEMA_NAME} CASCADE")

    lite = TradeJournal(tmp_path / "journal.db")
    pg = PostgresJournal(PG_DSN)
    yield lite, pg
    lite.close()
    pg.close()


def both(pair, method: str, *args, **kw):
    """Call the same method on both and return (sqlite_result, pg_result)."""
    lite, pg = pair
    return getattr(lite, method)(*args, **kw), getattr(pg, method)(*args, **kw)


def assert_same(pair, method: str, *args, **kw):
    a, b = both(pair, method, *args, **kw)
    assert scrub(a) == scrub(b), (
        f"{method}() differs:\n  sqlite: {scrub(a)}\n  pg    : {scrub(b)}"
    )
    return a, b


# ---------------------------------------------------------------------- #
# Signals
# ---------------------------------------------------------------------- #
def test_a_signal_round_trips_identically(pair):
    signal = make_signal()
    both(pair, "log_signal", signal, approved(), "pending_approval")
    assert_same(pair, "get_signal", signal.id)


def test_signal_fields_survive_the_trip(pair):
    """Including the JSON-encoded ones, which are easy to get wrong."""
    signal = make_signal(invalidation=["a", "b"], reasoning="x" * 500)
    both(pair, "log_signal", signal, approved(), "new", {"note": "hello"})
    a, b = assert_same(pair, "get_signal", signal.id)
    assert a["invalidation"] == '["a", "b"]'
    assert a["extra"] == '{"note": "hello"}'
    assert a["risk_approved"] == 1, "0/1 integer, not a boolean"


def test_a_rejected_signal_records_its_reason(pair):
    signal = make_signal()
    rejected = RiskDecision(approved=False, reasons=["daily loss limit"], sizing=None)
    both(pair, "log_signal", signal, rejected, "rejected")
    a, _ = assert_same(pair, "get_signal", signal.id)
    assert a["risk_approved"] == 0
    assert a["shares"] == 0.0, "no sizing means zero, not NULL"


def test_relogging_a_signal_overwrites_rather_than_duplicates(pair):
    """SQLite uses INSERT OR REPLACE; Postgres must upsert the same way."""
    signal = make_signal()
    both(pair, "log_signal", signal, approved(), "new")
    both(pair, "log_signal", signal, approved(), "submitted")
    a, b = assert_same(pair, "recent_signals", 50)
    assert len(a) == 1
    assert a[0]["status"] == "submitted"


def test_status_updates_match(pair):
    signal = make_signal()
    both(pair, "log_signal", signal, approved(), "new")
    both(pair, "update_signal_status", signal.id, "approved")
    assert_same(pair, "get_signal", signal.id)


def test_recent_signals_order_matches(pair):
    for i in range(5):
        both(pair, "log_signal", make_signal(symbol=f"SYM{i}"), approved(), "new")
    a, b = assert_same(pair, "recent_signals", 3)
    assert len(a) == 3


def test_an_unknown_signal_is_none_in_both(pair):
    assert_same(pair, "get_signal", "does_not_exist")


# ---------------------------------------------------------------------- #
# Trades — the money numbers
# ---------------------------------------------------------------------- #
def test_opening_a_trade_produces_the_same_id_and_row(pair):
    signal = make_signal()
    both(pair, "log_signal", signal, approved(), "new")
    ids = both(pair, "open_trade", signal, 50.0, "breakout")
    assert ids[0] == ids[1], "the trade id scheme must match exactly"
    assert_same(pair, "get_trade", ids[0])


def test_a_winning_trade_computes_identical_pnl_and_r(pair):
    """
    close_trade derives P&L and R-multiple itself. An early version of the
    Postgres port took them as arguments and would have recorded whatever
    it was handed.
    """
    signal = make_signal(entry=100.0, stop=98.0)
    both(pair, "log_signal", signal, approved(), "new")
    tid, _ = both(pair, "open_trade", signal, 50.0, "breakout")

    a, b = both(pair, "close_trade", tid, 104.0, "target", 1.5)
    assert scrub(a) == scrub(b)
    # 50 shares * $4 move = $200, less $1.50 fees.
    assert a["pnl"] == pytest.approx(198.5)
    # Risk was $2/share over 50 shares = $100 → 1.985R.
    assert a["r_multiple"] == pytest.approx(1.985)


def test_a_losing_trade_matches(pair):
    signal = make_signal(entry=100.0, stop=98.0)
    both(pair, "log_signal", signal, approved(), "new")
    tid, _ = both(pair, "open_trade", signal, 50.0, "breakout")
    a, b = both(pair, "close_trade", tid, 98.0, "stop", 1.0)
    assert scrub(a) == scrub(b)
    assert a["pnl"] == pytest.approx(-101.0)


def test_a_short_trade_matches(pair):
    """Direction flips the P&L sign; both must flip it the same way."""
    signal = make_signal(direction="short", entry=100.0, stop=102.0, target=95.0)
    both(pair, "log_signal", signal, approved(), "new")
    tid, _ = both(pair, "open_trade", signal, 20.0, "fade")
    a, b = both(pair, "close_trade", tid, 96.0, "target")
    assert scrub(a) == scrub(b)
    assert a["pnl"] == pytest.approx(80.0)


def test_closing_an_unknown_trade_is_none_in_both(pair):
    a, b = both(pair, "close_trade", "trd_nope", 100.0)
    assert a is None and b is None


def test_a_zero_risk_trade_does_not_divide_by_zero(pair):
    signal = make_signal(entry=100.0, stop=100.0)
    both(pair, "log_signal", signal, approved(), "new")
    tid, _ = both(pair, "open_trade", signal, 10.0)
    a, b = both(pair, "close_trade", tid, 101.0)
    assert scrub(a) == scrub(b)
    assert a["r_multiple"] == 0.0


def test_open_trades_and_stops_match(pair):
    for i, sym in enumerate(["AAPL", "MSFT", "NVDA"]):
        s = make_signal(symbol=sym, stop=90.0 + i)
        both(pair, "log_signal", s, approved(), "new")
        both(pair, "open_trade", s, 10.0)
    assert_same(pair, "open_trades")
    a, b = both(pair, "open_stops")
    assert a == b, "open_stops drives open-risk accounting"


def test_closed_trades_and_strategy_filter_match(pair):
    for i in range(4):
        s = make_signal(symbol=f"S{i}")
        both(pair, "log_signal", s, approved(), "new")
        tid, _ = both(pair, "open_trade", s, 10.0, "alpha" if i % 2 else "beta")
        both(pair, "close_trade", tid, 101.0 + i)
    assert_same(pair, "closed_trades", 500)
    assert_same(pair, "closed_trades", 500, "alpha")
    assert_same(pair, "closed_trades", 2)


def test_a_review_is_stored_the_same(pair):
    s = make_signal()
    both(pair, "log_signal", s, approved(), "new")
    tid, _ = both(pair, "open_trade", s, 10.0)
    both(pair, "save_review", tid, "took it too early")
    assert_same(pair, "get_trade", tid)


# ---------------------------------------------------------------------- #
# Equity — what the circuit breakers read
# ---------------------------------------------------------------------- #
def test_the_equity_curve_matches(pair):
    for i in range(6):
        both(pair, "record_equity", 10000.0 + i * 25, 5000.0, 2000.0)
    a, b = both(pair, "equity_curve", 1000)
    assert [r["equity"] for r in a] == [r["equity"] for r in b]
    assert [r["cash"] for r in a] == [r["cash"] for r in b]
    assert [r["equity"] for r in a] == sorted(r["equity"] for r in a), "ascending by ts"


def test_the_equity_curve_limit_takes_the_LATEST_rows(pair):
    """
    The nested select exists so a limit returns the most recent readings
    re-sorted ascending, not the oldest. Getting this backwards would
    draw the dashboard chart from ancient history.
    """
    for i in range(10):
        both(pair, "record_equity", 10000.0 + i, 0.0, 0.0)
    a, b = both(pair, "equity_curve", 3)
    assert [r["equity"] for r in a] == [r["equity"] for r in b]
    assert [r["equity"] for r in a] == [10007.0, 10008.0, 10009.0]


def test_start_of_day_equity_matches(pair):
    """
    The number the daily-loss breaker measures against. If this ever
    disagrees between backends, the breaker fires at the wrong level.
    """
    for i in range(4):
        both(pair, "record_equity", 10000.0 + i * 100, 0.0, 0.0)
    a, b = both(pair, "start_of_day_equity")
    assert a == b == 10000.0, "the FIRST reading of the day, not the last"


def test_start_of_day_equity_is_none_before_any_reading(pair):
    a, b = both(pair, "start_of_day_equity")
    assert a is None and b is None


def test_start_of_day_equity_ignores_other_days(pair):
    both(pair, "record_equity", 12345.0, 0.0, 0.0)
    a, b = both(pair, "start_of_day_equity", "1999-01-01")
    assert a is None and b is None
    today = datetime.now(timezone.utc).isoformat()[:10]
    a, b = both(pair, "start_of_day_equity", today)
    assert a == b == 12345.0


# ---------------------------------------------------------------------- #
# Runs and export
# ---------------------------------------------------------------------- #
def test_runs_round_trip(pair):
    lite, pg = pair
    a_id, b_id = both(pair, "start_run")
    assert isinstance(a_id, int) and isinstance(b_id, int)
    both(pair, "finish_run", a_id, 5, 2, 1, 0, "ok")


def test_the_csv_export_matches(pair, tmp_path):
    for i in range(3):
        s = make_signal(symbol=f"X{i}")
        both(pair, "log_signal", s, approved(), "new")
        tid, _ = both(pair, "open_trade", s, 10.0)
        both(pair, "close_trade", tid, 102.0)

    lite, pg = pair
    a = lite.export_csv(tmp_path / "a.csv").read_text(encoding="utf-8").splitlines()
    b = pg.export_csv(tmp_path / "b.csv").read_text(encoding="utf-8").splitlines()
    assert a[0] == b[0], "same header"
    assert len(a) == len(b), "same row count"


# ---------------------------------------------------------------------- #
# The metrics the dashboard reports
# ---------------------------------------------------------------------- #
def test_computed_metrics_agree(pair):
    """
    The end-to-end check: feed both the same trades, run the real metrics
    code over each, and compare what the dashboard would show.
    """
    from src.metrics import compute_metrics

    lite, pg = pair
    for i in range(6):
        s = make_signal(symbol=f"M{i}", entry=100.0, stop=98.0)
        both(pair, "log_signal", s, approved(), "new")
        tid, _ = both(pair, "open_trade", s, 25.0)
        both(pair, "close_trade", tid, 103.0 if i % 2 else 97.0)
    for i in range(5):
        both(pair, "record_equity", 10000.0 + i * 50, 0.0, 0.0)

    m_lite = compute_metrics(lite.closed_trades(limit=1000), lite.equity_curve()).to_dict()
    m_pg = compute_metrics(pg.closed_trades(limit=1000), pg.equity_curve()).to_dict()
    assert m_lite == m_pg
