"""
Choose the port the dashboard should listen on.

Prints one line: "<free-port> <state> <existing-instance-or-dash>", where
state is ok, already-ours, or none-free.

    python scripts/pick_port.py 8001 0.0.0.0

This lives in a file rather than a heredoc inside start.sh on purpose.
It used to be embedded as `PROBE="$(python - <<'PY' ... PY)"`, and macOS
ships bash 3.2, which scans a command substitution for its closing paren
with the ordinary quote lexer — it does not know a quoted heredoc body is
literal. One apostrophe in a Python comment ("don't") was therefore an
unterminated quote, and the whole script died with "unexpected EOF while
looking for matching quote" on a line far from the real cause. Bash 5
parses it correctly, so it passed every check here and broke on the user's
Mac. A plain file has no such trap, and can be tested directly.
"""
from __future__ import annotations

import json
import socket
import sys
import urllib.error
import urllib.request

# How far past the requested port to look before giving up.
SEARCH_RANGE = 40


def taken(port: int, host: str) -> bool:
    """
    Whether binding `port` would fail for the server.

    Binds exactly as uvicorn will: the same host, and WITHOUT SO_REUSEADDR.
    An earlier version bound 127.0.0.1 with SO_REUSEADDR while uvicorn binds
    0.0.0.0 without it. That is strictly more permissive, so it reported a
    busy port free and the server then died on it with "address already in
    use".
    """
    with socket.socket() as s:
        try:
            s.bind((host, port))
        except OSError:
            return True
    # A listener bound to a different interface blocks neither the bind
    # above nor anything else, so also check whether something answers.
    with socket.socket() as c:
        c.settimeout(0.2)
        try:
            c.connect(("127.0.0.1", port))
            return True
        except OSError:
            return False


def is_this_bot(port: int) -> bool:
    """Our own dashboard answering, rather than an unrelated app."""
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/api/health", timeout=0.6
        ) as response:
            return "execution_mode" in json.load(response)
    except (urllib.error.URLError, OSError, ValueError):
        return False


def pick(wanted: int, host: str) -> tuple[int, str, int | None]:
    """
    Find the first free port, and separately any copy of us already running.

    Both, because they are different answers. A copy already running means
    do not start a second one: two schedulers is double the scanning, double
    the API bill, and in auto mode double the orders. But if that is
    overridden, the second copy still needs a port that is actually free,
    not the one the first copy is sitting on.

    The whole range is scanned rather than stopping at the first gap: the
    running copy may itself have been bumped along by something else, so it
    can sit ABOVE the first free port.
    """
    existing: int | None = None
    chosen: int | None = None

    for candidate in range(wanted, wanted + SEARCH_RANGE):
        if taken(candidate, host):
            if existing is None and is_this_bot(candidate):
                existing = candidate
        elif chosen is None:
            chosen = candidate
        if existing is not None and chosen is not None:
            break

    if existing is not None:
        state = "already-ours"
    elif chosen is not None:
        state = "ok"
    else:
        state = "none-free"
    return (chosen if chosen is not None else wanted), state, existing


def main(argv: list[str]) -> int:
    wanted = int(argv[1]) if len(argv) > 1 and argv[1] else 8001
    host = argv[2] if len(argv) > 2 and argv[2] else "0.0.0.0"
    port, state, existing = pick(wanted, host)
    print(f"{port} {state} {existing if existing is not None else '-'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
