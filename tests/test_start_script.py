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
