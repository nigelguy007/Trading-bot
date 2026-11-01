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
    "$PY" -m venv "$VENV" || die "Could not create the virtual environment.
    On Debian/Ubuntu you may need:  sudo apt install python3-venv"
    ok "virtual environment created"
fi

VPY="$VENV/bin/python"
[ -x "$VPY" ] || die "The venv looks broken. Delete it and re-run:  rm -rf $VENV && ./start.sh"

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
COMMAND="${1:-serve}"

# Commands that talk to Claude. Fail these with one clear line rather than a
# 15-line Python traceback, which is what a missing key produces otherwise.
case "$COMMAND" in
    serve|scan|run|analyze|report|review|optimize|portfolio-review)
        if ! "$VPY" -c 'from src.config import get_settings; raise SystemExit(0 if get_settings().has_llm_key else 1)' 2>/dev/null; then
            printf '\n%s✗%s No Claude API key yet.\n\n' "$RED" "$OFF" >&2
            say "  1. Open the file:  $(pwd)/.env"
            say "  2. Find the line:  ANTHROPIC_API_KEY=your_anthropic_api_key_here"
            say "  3. Replace it with your key from https://console.anthropic.com"
            say ""
            say "  ${DIM}No key yet? These work without one:${OFF}"
            say "     ./start.sh backtest"
            say "     ./start.sh doctor"
            say ""
            exit 1
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
    PORT="$(grep -E '^WEBHOOK_PORT=' .env 2>/dev/null | cut -d= -f2 | tr -d '[:space:]')"
    PORT="${PORT:-8001}"
    say ""
    ok "Starting the dashboard"
    say "  Open this in your browser:  ${GREEN}http://localhost:${PORT}${OFF}"
    say "  ${DIM}Press Ctrl-C here to stop it.${OFF}"
    say ""
fi

exec "$VPY" -m src.cli "$@"
