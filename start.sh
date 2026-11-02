#!/usr/bin/env bash
#
# One-command launcher. Works from any directory, on macOS and Linux.
#
#   ./start.sh              # starts the dashboard at http://localhost:8001
#   ./start.sh doctor       # checks your setup
#   ./start.sh scan         # one pass over the watchlist
#   ./start.sh backtest
#
# It exists because `python -m src.cli` only works from the project root with
# the right interpreter, and getting either wrong produces an error message
# ("No module named 'src'") that doesn't say so.

set -euo pipefail

# Always operate from the project root, however the script was invoked.
cd "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

VENV="venv"
STAMP="$VENV/.deps-installed"
GREEN=$'\033[32m'; YELLOW=$'\033[33m'; RED=$'\033[31m'; DIM=$'\033[2m'; OFF=$'\033[0m'

say()  { printf '%s\n' "$*"; }
ok()   { printf '%s✓%s %s\n' "$GREEN" "$OFF" "$*"; }
warn() { printf '%s!%s %s\n' "$YELLOW" "$OFF" "$*"; }
die()  { printf '%s✗%s %s\n' "$RED" "$OFF" "$*" >&2; exit 1; }

# --- 1. Find a Python 3.11+ ------------------------------------------------
PY=""
for candidate in python3.13 python3.12 python3.11 python3 python; do
    if command -v "$candidate" >/dev/null 2>&1; then
        if "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)' 2>/dev/null; then
            PY="$candidate"; break
        fi
    fi
done

if [ -z "$PY" ]; then
    die "Need Python 3.11 or newer.
    macOS:  brew install python@3.12
    Linux:  sudo apt install python3.12 python3.12-venv"
fi

# --- 2. Create the virtual environment if missing --------------------------
# A venv keeps this project's packages separate from your system Python, so
# installing them can't break anything else on your machine.
if [ ! -d "$VENV" ]; then
    say "${DIM}First run: creating a virtual environment (one time, ~10s)...${OFF}"
    if ! "$PY" -m venv "$VENV" >/dev/null 2>&1; then
        # The default venv layout uses symlinks, which exFAT and FAT32 don't
        # support — the usual format on external drives shared with Windows.
        # --copies writes real files instead.
        rm -rf "$VENV"
        say "${DIM}Retrying without symlinks (normal on an external drive)...${OFF}"
        "$PY" -m venv --copies "$VENV" || die "Could not create the virtual environment.
    On Debian/Ubuntu you may need:  sudo apt install python3-venv
    On an external drive, check it is not mounted read-only."
    fi
    ok "virtual environment created"
fi

VPY="$VENV/bin/python"
if [ ! -x "$VPY" ]; then
    # On a FAT-formatted drive the execute bit may not stick. Calling the
    # interpreter through the shell still works, so only fail if it is absent.
    [ -f "$VPY" ] || die "The venv looks broken. Delete it and re-run:
    rm -rf $VENV && bash start.sh"
fi

# --- 3. Install dependencies when they're missing or out of date -----------
if [ ! -f "$STAMP" ] || [ requirements-trader.txt -nt "$STAMP" ]; then
    say "${DIM}Installing dependencies (one time, ~30s)...${OFF}"
    "$VPY" -m pip install --quiet --upgrade pip
    "$VPY" -m pip install --quiet -r requirements-trader.txt || die "Dependency install failed (see above)."
    date > "$STAMP"
    ok "dependencies installed"
fi

# --- 4. Make sure a .env exists --------------------------------------------
if [ ! -f .env ]; then
    cp .env.example .env
    warn "Created .env from the template."
    say  "  Open it and add your ANTHROPIC_API_KEY before anything will analyse."
    say  "  Until then you can still run:  ./start.sh backtest"
    say  ""
fi

# --- 5. Run ----------------------------------------------------------------
# Default to `serve` when invoked bare. This must rewrite the argument list,
# not just a variable: the final exec forwards "$@", so setting COMMAND alone
# left Python with no command at all — it printed usage and exited while the
# banner above had already claimed the dashboard was starting.
if [ "$#" -eq 0 ]; then
    set -- serve
fi
COMMAND="$1"

# Write a key into .env without the user having to find and edit a hidden
# dotfile. Done in Python so a key containing / or \ can't corrupt the file.
save_api_key() {
    "$VPY" - "$1" <<'PY'
import pathlib, re, sys
key = sys.argv[1].strip()
path = pathlib.Path(".env")
text = path.read_text(encoding="utf-8")
line = "ANTHROPIC_API_KEY=" + key
if re.search(r"^ANTHROPIC_API_KEY=.*$", text, flags=re.M):
    # A lambda replacement keeps backslashes in the key literal.
    text = re.sub(r"^ANTHROPIC_API_KEY=.*$", lambda _m: line, text, count=1, flags=re.M)
else:
    text = text.rstrip("\n") + "\n" + line + "\n"
path.write_text(text, encoding="utf-8")
PY
}

prompt_for_api_key() {
    printf '\n%s✗%s No Claude API key yet.\n\n' "$RED" "$OFF"
    say "  Get one at ${GREEN}https://console.anthropic.com${OFF} → API Keys."
    say "  It starts with ${DIM}sk-ant-${OFF}"
    say ""

    # No terminal to read from (CI, piped input): print instructions instead.
    if [ ! -t 0 ]; then
        say "  Then add it to this file:  $(pwd)/.env"
        say "  on the line:               ANTHROPIC_API_KEY=..."
        say ""
        return 1
    fi

    printf '  Paste it here and press Enter (input is hidden, nothing is echoed).\n'
    printf '  Or just press Enter to skip.\n\n'
    printf '  Key: '
    read -r -s NEWKEY || true
    printf '\n\n'

    NEWKEY="$(printf '%s' "$NEWKEY" | tr -d '[:space:]')"

    if [ -z "$NEWKEY" ]; then
        say "  Skipped. These still work without a key:"
        say "     ./start.sh backtest"
        say "     ./start.sh doctor"
        say ""
        return 1
    fi

    case "$NEWKEY" in
        sk-ant-*) ;;
        *)  warn "That doesn't look like an Anthropic key (they begin 'sk-ant-')."
            say  "  Saving it anyway — run ./start.sh doctor --check-apis to test it."
            ;;
    esac

    save_api_key "$NEWKEY"
    ok "Saved to .env  ${DIM}(${#NEWKEY} characters, ending ...${NEWKEY: -4})${OFF}"
    say ""
    return 0
}

# Commands that talk to Claude. Offer to fix a missing key on the spot rather
# than failing with a 15-line Python traceback from inside the web server.
needs_key() {
    "$VPY" -c 'from src.config import get_settings; raise SystemExit(0 if get_settings().has_llm_key else 1)' 2>/dev/null
}

case "$COMMAND" in
    setkey)
        prompt_for_api_key || exit 1
        exec "$VPY" -m src.cli doctor
        ;;
    scan|run|analyze|report|review|optimize|portfolio-review)
        if ! needs_key; then
            prompt_for_api_key || exit 1
        fi
        ;;
    backtest)
        # Backtesting without broker keys reads data/csv/*.csv. On a fresh
        # clone there are none, and the run silently tests zero bars — so
        # generate the sample set rather than reporting an empty result.
        if ! compgen -G "data/csv/*.csv" > /dev/null; then
            say "${DIM}No price data yet — generating a synthetic sample set...${OFF}"
            # Cover the configured watchlist, so nothing is reported as skipped.
            SYMS="$("$VPY" -c 'from src.config import get_settings; print(",".join(get_settings().all_symbols))' 2>/dev/null)"
            if [ -n "$SYMS" ]; then
                "$VPY" scripts/make_sample_data.py --symbols "$SYMS" >/dev/null
            else
                "$VPY" scripts/make_sample_data.py >/dev/null
            fi
            warn "Sample data is RANDOM. It proves the pipeline runs; it says"
            say  "  nothing about any strategy. Use real data before believing a number."
            say  ""
        fi
        ;;
esac

if [ "$COMMAND" = "serve" ]; then
    WANTED="$(grep -E '^WEBHOOK_PORT=' .env 2>/dev/null | cut -d= -f2 | tr -d '[:space:]')"
    WANTED="${WANTED:-8001}"

    # Another app may already own the port — 8000/8001 are popular. Binding
    # second fails, or worse the browser shows you the other app and you think
    # this one is broken. Find a free port and say which one we took.
    PORT="$("$VPY" - "$WANTED" <<'PY'
import socket, sys
wanted = int(sys.argv[1])
def free(port):
    with socket.socket() as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False
for candidate in [wanted, *range(wanted + 1, wanted + 40)]:
    if free(candidate):
        print(candidate)
        break
else:
    print(wanted)
PY
)"

    say ""
    if [ "$PORT" != "$WANTED" ]; then
        warn "Port $WANTED is already in use by another program."
        say  "  Using port ${PORT} instead."
        say  ""
    fi
    ok "Starting the dashboard"
    say "  Open this in your browser:  ${GREEN}http://localhost:${PORT}${OFF}"
    say "  ${DIM}Press Ctrl-C here to stop it.${OFF}"
    say ""
    export WEBHOOK_PORT="$PORT"
fi

exec "$VPY" -m src.cli "$@"
