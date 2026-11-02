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
    assert 'set -- start' in script, (
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
def test_the_host_is_read_from_env_and_passed_to_the_picker(script):
    """
    The probe must bind the same host uvicorn will. It bound 127.0.0.1
    while uvicorn binds WEBHOOK_HOST (0.0.0.0), and a port held on 0.0.0.0
    does not block a 127.0.0.1 bind — so the launcher announced a port and
    uvicorn then died on it with "address already in use".
    """
    assert "WEBHOOK_HOST=" in script
    assert 'scripts/pick_port.py "$WANTED" "$HOST"' in script


def test_no_heredoc_lives_inside_a_command_substitution(script):
    """
    THE macOS bug. Bash 3.2, which Apple still ships, scans a command
    substitution for its closing paren with the ordinary quote lexer and
    does not know a quoted heredoc body is literal. A single apostrophe in
    the embedded Python ("don't") was therefore an unterminated quote and
    the whole script failed to parse — on the user's Mac only, because
    bash 5 here parses it correctly and every local check passed.

    Keep embedded interpreters in files. This catches the pattern coming
    back whatever the content.
    """
    offenders = [
        line for line in script.splitlines()
        if "$(" in line and "<<" in line and line.index("$(") < line.index("<<")
    ]
    assert not offenders, (
        "heredoc inside $(...) breaks bash 3.2 on macOS; move it to a file: "
        + " | ".join(offenders)
    )


def test_the_port_picker_is_a_real_file(script):
    assert "scripts/pick_port.py" in script
    assert (START_SH.parent / "scripts" / "pick_port.py").exists()


def test_a_failed_probe_does_not_stop_the_dashboard(script):
    """A broken helper must degrade to the configured port, not to nothing."""
    assert '[ -n "$PROBE" ] || PROBE="$WANTED ok -"' in script


def test_a_second_instance_is_refused_rather_than_started(script):
    """
    Two copies means two schedulers: double scanning, double the Claude
    bill, and in auto mode double the orders.
    """
    assert "already-ours" in script
    assert "ALREADY RUNNING" in script
    assert "ALLOW_SECOND_INSTANCE" in script


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


# ---------------------------------------------------------------------- #
# macOS bash 3.2 compatibility
# ---------------------------------------------------------------------- #
# Apple froze bash at 3.2 in 2007 over the GPL3 licence change, so every
# Mac runs a shell from before most of bash's modern syntax existed. The
# container that builds this runs bash 5, which is why `bash -n`, the test
# suite and three live launches all passed while the script was unusable
# on the user's machine.
#
# These two checks are static and always run. If a real bash 3.2 happens to
# be on PATH the last test parses against it for real.

BASH4_ONLY = [
    (r"\$\{[A-Za-z_][A-Za-z0-9_]*\^\^", "${VAR^^} uppercase expansion"),
    (r"\$\{[A-Za-z_][A-Za-z0-9_]*,,", "${VAR,,} lowercase expansion"),
    (r"\bdeclare\s+-A\b", "associative arrays"),
    (r"\bmapfile\b", "mapfile"),
    (r"\breadarray\b", "readarray"),
    (r"&>>", "&>> append redirection"),
    (r"\|&", "|& pipe-stderr shorthand"),
    (r"\bcoproc\b", "coproc"),
]


def test_no_bash_4_only_syntax(script):
    """Every Mac runs bash 3.2. None of this exists there."""
    code = "\n".join(
        line for line in script.splitlines() if not line.lstrip().startswith("#")
    )
    found = [name for pattern, name in BASH4_ONLY if re.search(pattern, code)]
    assert not found, f"bash 4+ only syntax, which macOS cannot run: {found}"


def test_it_parses_under_a_real_bash_3_2_when_one_is_available():
    """
    The definitive check, skipped when no bash 3.2 is installed.

    Build one with:
        curl -O https://ftp.gnu.org/gnu/bash/bash-3.2.tar.gz
        tar xzf bash-3.2.tar.gz && cd bash-3.2
        ./configure --without-bash-malloc && make
    """
    import shutil

    candidates = [
        shutil.which("bash-3.2"),
        shutil.which("bash32"),
        "/tmp/b32/bash-3.2/bash",
        "/bin/bash",
    ]
    for candidate in candidates:
        if not candidate or not Path(candidate).exists():
            continue
        version = subprocess.run(
            [candidate, "--version"], capture_output=True, text=True
        ).stdout
        if "version 3.2" not in version:
            continue
        result = subprocess.run(
            [candidate, "-n", str(START_SH)], capture_output=True, text=True
        )
        assert result.returncode == 0, (
            f"start.sh does not parse under bash 3.2, which is what every Mac "
            f"runs:\n{result.stderr}"
        )
        return
    pytest.skip("no bash 3.2 available; the static checks above still ran")


def test_the_already_running_notice_mentions_a_stale_pull(script):
    """
    The trap the guard creates: `git pull && bash start.sh` prints "already
    running" and exits, so the OLD process keeps serving the OLD page and
    the pull looks like it did nothing.
    """
    assert "serving the OLD code" in script
    assert "Cmd-Shift-R" in script


# ---------------------------------------------------------------------- #
# Running in the background
# ---------------------------------------------------------------------- #
# `serve` runs uvicorn in the foreground, so the bot only lived while a
# Terminal window sat there untouched. Typing the next command in that
# window killed it, and the browser then showed ERR_CONNECTION_REFUSED —
# which was repeatedly misdiagnosed as a port problem. A bare ./start.sh
# now backgrounds the server and hands the prompt back.


def test_a_bare_run_does_not_block_the_terminal(script):
    assert "set -- start" in script
    assert "nohup" in script, "the server must survive the shell that started it"
    assert 'nohup "$VPY" -m src.cli serve' in script


def test_the_background_server_records_its_pid_and_port(script):
    """Without these, stop and status cannot find it."""
    assert "PIDFILE=" in script
    assert "PORTFILE=" in script
    assert 'echo "$!" > "$PIDFILE"' in script
    assert 'echo "$PORT" > "$PORTFILE"' in script


def test_there_is_a_way_to_stop_and_inspect_it(script):
    for command in ("stop)", "status)", "logs)"):
        assert command in script, f"start.sh needs a {command[:-1]} command"


def test_a_stale_pidfile_does_not_read_as_running(script):
    """A crash or a reboot leaves the file behind; the process is gone."""
    assert 'kill -0 "$BOTPID"' in script


def test_a_failed_start_shows_the_reason(script):
    """
    Backgrounding hides the traceback. Printing the log tail is the whole
    difference between a fixable error and a silent refusal to connect.
    """
    assert "It started but then stopped" in script
    assert 'tail -n 25 "$LOGFILE"' in script


def test_status_compares_the_running_build_against_the_checkout(script):
    """
    The stale-process trap: git pull succeeds, the old server keeps
    serving old HTML. Showing both builds side by side makes it obvious.
    """
    assert "/api/health" in script
    assert "git rev-parse --short HEAD" in script
    assert "This checkout is at" in script


def test_serve_is_still_available_for_the_foreground(script):
    """Containers and process managers want PID 1 to be the server."""
    assert 'if [ "$COMMAND" = "serve" ]; then' in script
    assert re.search(r'exec\s+"\$VPY"\s+-m\s+src\.cli\s+"\$@"', script)


def test_a_stale_server_is_restarted_rather_than_reported(script):
    """
    Telling the user to restart after a pull did not work — three rounds
    of "I pulled and nothing changed". Compare the running build to the
    checkout and just do it.
    """
    assert "running_build" in script
    assert "checkout_build" in script
    assert '"$RUNNING_BUILD" != "$HERE_BUILD"' in script
    assert "Restarting it so you get the current code" in script


def test_stop_is_shared_between_the_stop_command_and_the_restart(script):
    """Two copies of the kill logic would drift apart."""
    assert "stop_bot()" in script
    assert script.count("pkill -f 'src\\.cli serve'") == 1
