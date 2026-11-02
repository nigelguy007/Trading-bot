"""
Port selection.

This was a heredoc inside start.sh until it broke on a real Mac. It is a
file now partly so it cannot break that way again, and partly so it can be
tested — which it could not be while it lived inside a shell string.
"""
from __future__ import annotations

import socket
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts.pick_port import pick, taken  # noqa: E402

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "pick_port.py"


@pytest.fixture
def held():
    """Hold ports open for the duration of a test, exactly as a server would."""
    sockets: list[socket.socket] = []

    def _hold(port: int) -> None:
        s = socket.socket()
        s.bind(("0.0.0.0", port))
        s.listen(1)
        sockets.append(s)

    yield _hold
    for s in sockets:
        s.close()


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


# ---------------------------------------------------------------------- #
def test_a_free_port_is_returned_as_is():
    port = free_port()
    chosen, state, existing = pick(port, "0.0.0.0")
    assert chosen == port
    assert state == "ok"
    assert existing is None


def test_an_occupied_port_is_stepped_over(held):
    port = free_port()
    held(port)
    chosen, state, _ = pick(port, "0.0.0.0")
    assert chosen == port + 1
    assert state == "ok"


def test_several_occupied_ports_are_stepped_over(held):
    port = free_port()
    for offset in range(3):
        held(port + offset)
    chosen, _, _ = pick(port, "0.0.0.0")
    assert chosen == port + 3


def test_taken_agrees_with_an_actual_bind(held):
    port = free_port()
    assert not taken(port, "0.0.0.0")
    held(port)
    assert taken(port, "0.0.0.0")


def test_an_unrelated_server_is_not_mistaken_for_this_bot(held):
    """
    The whole point of the /api/health check: another app on the port must
    step the search along, not trigger "already running".
    """
    port = free_port()
    held(port)
    _, state, existing = pick(port, "0.0.0.0")
    assert state == "ok"
    assert existing is None


def test_the_output_line_has_three_fields():
    result = subprocess.run(
        [sys.executable, str(SCRIPT), str(free_port()), "0.0.0.0"],
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0
    fields = result.stdout.split()
    assert len(fields) == 3
    assert fields[0].isdigit()
    assert fields[1] in {"ok", "already-ours", "none-free"}


def test_it_runs_with_no_arguments():
    result = subprocess.run(
        [sys.executable, str(SCRIPT)], capture_output=True, text=True, timeout=60
    )
    assert result.returncode == 0
    assert len(result.stdout.split()) == 3


def test_the_source_has_no_syntax_errors_under_a_bare_interpreter():
    """It is run by the venv python directly, with no package context."""
    result = subprocess.run(
        [sys.executable, "-m", "py_compile", str(SCRIPT)],
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stderr


# ---------------------------------------------------------------------- #
# An instance of this bot already running
# ---------------------------------------------------------------------- #
def test_a_running_copy_is_reported_instead_of_a_free_port(held, monkeypatch):
    """
    Two copies means two schedulers: double scanning, double the API bill,
    and in auto mode double the orders.
    """
    import scripts.pick_port as pp

    port = free_port()
    held(port)
    monkeypatch.setattr(pp, "is_this_bot", lambda p: p == port)

    chosen, state, existing = pp.pick(port, "0.0.0.0")
    assert state == "already-ours"
    assert existing == port
    # And the free port is still reported separately, because an override
    # needs somewhere to actually bind. The first cut handed the second copy
    # the port the first was already on, and it died on bind.
    assert chosen != port
    assert not taken(chosen, "0.0.0.0")


def test_a_copy_running_above_the_first_gap_is_still_found(held, monkeypatch):
    """
    Stopping at the first free port would miss a running copy sitting ABOVE
    the gap — which is exactly where it ends up once something else has
    bumped it along.
    """
    import scripts.pick_port as pp

    base = free_port()
    held(base)          # something unrelated
    ours = base + 2
    held(ours)          # our own copy, past the gap at base + 1
    monkeypatch.setattr(pp, "is_this_bot", lambda p: p == ours)

    chosen, state, existing = pp.pick(base, "0.0.0.0")
    assert state == "already-ours"
    assert existing == ours
    assert chosen == base + 1


def test_a_full_range_is_reported_rather_than_guessed(monkeypatch):
    import scripts.pick_port as pp

    monkeypatch.setattr(pp, "taken", lambda p, h: True)
    monkeypatch.setattr(pp, "is_this_bot", lambda p: False)
    chosen, state, existing = pp.pick(9000, "0.0.0.0")
    assert state == "none-free"
    assert existing is None
    assert chosen == 9000


# ---------------------------------------------------------------------- #
# TIME_WAIT
# ---------------------------------------------------------------------- #
def test_a_just_released_port_is_offered_again():
    """
    The restart bug: the launcher stops the server and starts it again, but
    the port it just let go sits in TIME_WAIT. uvicorn can rebind it because
    asyncio sets SO_REUSEADDR; a probe without that flag cannot, so the
    launcher stepped to the next port and the dashboard URL moved on every
    single restart — 8001, 8002, 8003 — breaking any open tab.

    Drive a real connection through the socket so the listening side
    actually enters TIME_WAIT, then insist the port reads as free.
    """
    port = free_port()
    server = socket.socket()
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", port))
    server.listen(1)

    client = socket.socket()
    client.connect(("127.0.0.1", port))
    accepted, _ = server.accept()
    for s in (client, accepted, server):
        s.close()

    assert not taken(port, "127.0.0.1"), (
        "a port in TIME_WAIT must be offered again — uvicorn can bind it"
    )


def test_the_probe_sets_the_same_socket_option_uvicorn_does():
    """If these drift apart the probe stops predicting the real bind."""
    import inspect

    import scripts.pick_port as pp

    assert "SO_REUSEADDR" in inspect.getsource(pp.taken)


def test_a_live_listener_is_still_taken(held):
    """SO_REUSEADDR must not make an occupied port look free."""
    port = free_port()
    held(port)
    assert taken(port, "0.0.0.0")
