"""
The launcher script's argument handling.

These exist because a bare `./start.sh` once printed "Starting the dashboard",
then exited with an argparse usage error — the banner claimed success while
nothing was listening. The bug was that the default command was written to a
variable but never into the argument list that gets forwarded to Python.

Checked by parsing the script rather than executing it: running it for real
builds a virtual environment and downloads packages, which is far too slow and
network-dependent for a unit test.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

START_SH = Path(__file__).resolve().parent.parent / "start.sh"


@pytest.fixture(scope="module")
def script() -> str:
    return START_SH.read_text(encoding="utf-8")


def test_start_sh_exists_and_is_bash(script):
    assert START_SH.exists()
    assert script.startswith("#!/usr/bin/env bash")


def test_script_is_syntactically_valid():
    result = subprocess.run(
        ["bash", "-n", str(START_SH)], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr


def test_bare_invocation_rewrites_the_argument_list(script):
    """
    The regression. `COMMAND="${1:-serve}"` alone is not enough — the final
    exec forwards "$@", so the argument list itself must be set.
    """
    assert 'set -- serve' in script, (
        "start.sh must put the default command into $@, not only into a variable"
    )
    assert 'COMMAND="${1:-serve}"' not in script, (
        "that form defaults the variable but leaves $@ empty, so Python gets no command"
    )


def test_default_command_reaches_python():
    """Simulate the argument handling in isolation: no args must yield 'serve'."""
    snippet = 'if [ "$#" -eq 0 ]; then set -- serve; fi; echo "$@"'
    out = subprocess.run(["bash", "-c", snippet], capture_output=True, text=True)
    assert out.stdout.strip() == "serve"

    out = subprocess.run(
        ["bash", "-c", snippet, "_", "doctor"], capture_output=True, text=True
    )
    assert out.stdout.strip() == "doctor"


def test_it_execs_with_the_forwarded_arguments(script):
    assert re.search(r'exec\s+"\$VPY"\s+-m\s+src\.cli\s+"\$@"', script), (
        "the launcher must forward $@ to the CLI"
    )


def test_cd_to_script_directory_is_quoted(script):
    """Drive names contain spaces; an unquoted cd breaks on an external disk."""
    assert 'cd "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"' in script


def test_venv_falls_back_to_copies(script):
    """exFAT drives cannot store the symlink a default venv creates."""
    assert "--copies" in script


def test_serve_does_not_require_an_api_key(script):
    """The dashboard must open without API billing set up."""
    key_gated = re.search(r"^\s*(scan\|run\|analyze[^)]*)\)", script, re.M)
    assert key_gated, "expected a case branch listing the key-gated commands"
    assert "serve" not in key_gated.group(1), (
        "serve must not be gated behind an API key — the dashboard works without one"
    )


def test_port_collision_is_handled(script):
    """8001 is commonly taken; binding second shows the other app's page."""
    assert "already in use" in script
    assert "WEBHOOK_PORT" in script


# ---------------------------------------------------------------------- #
# Port selection
# ---------------------------------------------------------------------- #
def test_the_probe_binds_the_same_host_uvicorn_will(script):
    """
    The regression that shipped: the probe bound 127.0.0.1 while uvicorn
    binds WEBHOOK_HOST (0.0.0.0). A port held on 0.0.0.0 did not block a
    127.0.0.1 bind, so the launcher announced a port and uvicorn then died
    on it with "address already in use".
    """
    assert 'HOST="$(grep -E \'^WEBHOOK_HOST=\' .env' in script
    assert 's.bind((host, port))' in script
    assert 's.bind(("127.0.0.1", port))' not in script


def test_the_probe_does_not_set_so_reuseaddr(script):
    """
    SO_REUSEADDR made the probe strictly more permissive than uvicorn's own
    bind, so it reported a busy port free. It must fail wherever uvicorn
    would fail.

    Comments are stripped first: the fix is explained in one, and the
    explanation must not be what satisfies the test.
    """
    code = "\n".join(
        line for line in script.splitlines() if not line.lstrip().startswith("#")
    )
    assert "SO_REUSEADDR" not in code
    assert "setsockopt" not in code


def test_a_second_instance_is_refused_rather_than_started(script):
    """
    Two copies means two schedulers: double scanning, double the Claude
    bill, and in auto mode double the orders.
    """
    assert "already-ours" in script
    assert "ALREADY RUNNING" in script
    assert "ALLOW_SECOND_INSTANCE" in script


def test_the_whole_range_is_scanned_for_an_existing_copy(script):
    """
    Stopping at the first free port would miss a running copy sitting ABOVE
    the gap, which is exactly what happens once something else has bumped
    it along.
    """
    assert "if existing is not None and chosen is not None:" in script


def test_the_override_gets_a_free_port_not_the_occupied_one(script):
    """
    First cut of the fix handed ALLOW_SECOND_INSTANCE the port the running
    copy was already on, so the override died on bind. The probe reports a
    free port and the existing one separately.
    """
    assert 'RUNNING_ON=' in script
    assert 'http://localhost:${RUNNING_ON}' in script
    assert 'export WEBHOOK_PORT="$PORT"' in script


def test_running_out_of_ports_fails_loudly(script):
    assert "none-free" in script
    assert "are all in use" in script
