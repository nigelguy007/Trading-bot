"""
The scan loop that lives inside the web server.

This exists because `./start.sh` runs `serve`, and `serve` used to be the
dashboard alone — the scan loop was in a separate `run` command. So the
ordinary way to start the bot produced a page that displayed an idle
account forever and never analysed anything, with nothing saying so.
"""
from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import pytest

from src.runner import BackgroundScanner, run_one_cycle


def make_ctx(
    *,
    has_key: bool = True,
    market_open: bool = True,
    market_hours_only: bool = True,
    cycle=None,
    reconcile=None,
) -> SimpleNamespace:
    summary = SimpleNamespace(summary=lambda: "scanned 3, signals 1")
    return SimpleNamespace(
        settings=SimpleNamespace(
            has_llm_key=has_key,
            scan_interval_minutes=15,
            execution_mode="alert",
            market_hours_only=market_hours_only,
            symbols=["AAPL"],
            crypto_symbols=[],
        ),
        broker=SimpleNamespace(is_market_open=lambda: market_open),
        execution=SimpleNamespace(reconcile=reconcile or (lambda: {})),
        pipeline=SimpleNamespace(run_cycle=cycle or (lambda: summary)),
    )


@pytest.fixture
def scanner():
    s = BackgroundScanner(make_ctx())
    yield s
    s.shutdown()


# ---------------------------------------------------------------------- #
# State
# ---------------------------------------------------------------------- #
def test_it_does_not_run_until_asked(scanner):
    """
    Every cycle costs Claude API calls, and in auto mode it places
    orders. Switching it on is an explicit act.
    """
    assert scanner.running is False
    assert scanner.state()["running"] is False


def test_the_state_says_everything_the_ui_needs(scanner):
    state = scanner.state()
    for key in ("running", "busy", "interval_minutes", "execution_mode",
                "next_run", "last_run", "market_open", "market_hours_only"):
        assert key in state, key


def test_starting_schedules_a_repeating_job(scanner):
    ok, message = scanner.start()
    assert ok, message
    assert scanner.running
    assert "15 min" in message
    assert scanner.state()["next_run"] is not None


def test_starting_twice_is_refused_rather_than_doubled(scanner):
    """Two jobs means two scans per interval and double the API bill."""
    assert scanner.start()[0] is True
    ok, message = scanner.start()
    assert ok is False
    assert "Already running" in message


def test_stopping_works_and_is_idempotent(scanner):
    scanner.start()
    ok, message = scanner.stop()
    assert ok is True
    assert "stops are untouched" in message
    assert scanner.running is False
    assert scanner.stop()[0] is False


def test_it_refuses_to_start_without_a_claude_key():
    """There is nothing to analyse with, so a running loop would be a lie."""
    s = BackgroundScanner(make_ctx(has_key=False))
    try:
        ok, message = s.start()
        assert ok is False
        assert "setkey" in message
        assert s.running is False
    finally:
        s.shutdown()


# ---------------------------------------------------------------------- #
# Running a cycle
# ---------------------------------------------------------------------- #
def test_a_cycle_records_its_summary():
    s = BackgroundScanner(make_ctx())
    try:
        s._run_and_record()
        last = s.state()["last_run"]
        assert last["error"] is None
        assert last["summary"] == "scanned 3, signals 1"
        assert last["at"]
    finally:
        s.shutdown()


def test_a_failed_cycle_is_recorded_and_does_not_raise():
    """A broker outage must not kill the loop; the next cycle may work."""
    def boom():
        raise RuntimeError("broker unreachable")

    s = BackgroundScanner(make_ctx(cycle=boom))
    try:
        s._run_and_record()
        last = s.state()["last_run"]
        assert last["summary"] is None
        assert "broker unreachable" in last["error"]
        assert s.state()["busy"] is False
    finally:
        s.shutdown()


def test_two_cycles_cannot_overlap():
    """
    A slow Claude call must not let the next tick start a second pass
    over the same symbols — that double-analyses and double-bills.
    """
    started = threading.Event()
    release = threading.Event()
    calls = []

    def slow():
        calls.append(1)
        started.set()
        release.wait(timeout=5)
        return SimpleNamespace(summary=lambda: "slow")

    s = BackgroundScanner(make_ctx(cycle=slow))
    try:
        first = threading.Thread(target=s._run_and_record)
        first.start()
        assert started.wait(timeout=5)
        # The second call must return immediately, not queue behind it.
        s._run_and_record()
        assert len(calls) == 1
        release.set()
        first.join(timeout=5)
    finally:
        release.set()
        s.shutdown()


def test_the_market_being_shut_skips_the_cycle():
    ran = []
    s = BackgroundScanner(make_ctx(
        market_open=False,
        cycle=lambda: ran.append(1) or SimpleNamespace(summary=lambda: ""),
    ))
    try:
        s._job()
        assert ran == []
        assert s.state()["last_run"] is None
    finally:
        s.shutdown()


def test_market_hours_can_be_ignored():
    ran = []
    s = BackgroundScanner(make_ctx(
        market_open=False,
        market_hours_only=False,
        cycle=lambda: ran.append(1) or SimpleNamespace(summary=lambda: "ok"),
    ))
    try:
        s._job()
        assert ran == [1]
    finally:
        s.shutdown()


def test_reconcile_runs_before_the_scan():
    """
    Trading 212 has no OCO, so a stop can be left resting after a target
    fills. Repair that before opening anything new.
    """
    order = []
    ctx = make_ctx(
        reconcile=lambda: order.append("reconcile") or {},
        cycle=lambda: order.append("scan") or SimpleNamespace(summary=lambda: "x"),
    )
    run_one_cycle(ctx)
    assert order == ["reconcile", "scan"]


def test_scan_now_runs_even_while_paused():
    s = BackgroundScanner(make_ctx())
    try:
        ok, _ = s.scan_now()
        assert ok is True
        for _ in range(50):
            if s.state()["last_run"]:
                break
            time.sleep(0.1)
        assert s.state()["last_run"] is not None
        assert s.running is False
    finally:
        s.shutdown()


def test_scan_now_needs_a_key():
    s = BackgroundScanner(make_ctx(has_key=False))
    try:
        ok, message = s.scan_now()
        assert ok is False
        assert "setkey" in message
    finally:
        s.shutdown()
