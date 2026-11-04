#!/usr/bin/env bash
#
# Deploy the trading bot to Fly.io, in one command, from the machine that
# already has your keys.
#
#   bash scripts/deploy-fly.sh                 # uses the app name in fly.toml
#   bash scripts/deploy-fly.sh my-desk-name    # or pick your own
#
# Your secrets go straight from .env to Fly. They are never printed, never
# committed, and never leave your machine except to Fly itself.
#
# Safe to re-run: it creates the app and the volume only if they are
# missing, and a second run is just a redeploy.

set -euo pipefail
cd "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

GREEN=$'\033[32m'; YELLOW=$'\033[33m'; RED=$'\033[31m'; DIM=$'\033[2m'; OFF=$'\033[0m'
say()  { printf '%s\n' "$*"; }
ok()   { printf '%s✓%s %s\n' "$GREEN" "$OFF" "$*"; }
warn() { printf '%s!%s %s\n' "$YELLOW" "$OFF" "$*"; }
die()  { printf '%s✗%s %s\n' "$RED" "$OFF" "$*" >&2; exit 1; }

REGION="${FLY_REGION:-lhr}"
VOLUME="trader_data"

# --- 1. Tools and sign-in --------------------------------------------- #
if ! command -v flyctl >/dev/null 2>&1; then
    if [ -x "$HOME/.fly/bin/flyctl" ]; then
        PATH="$HOME/.fly/bin:$PATH"
    else
        die "flyctl is not installed. Install it, then re-run:
    curl -L https://fly.io/install.sh | sh
    export PATH=\"\$HOME/.fly/bin:\$PATH\""
    fi
fi

if ! flyctl auth whoami >/dev/null 2>&1; then
    die "Not signed in to Fly. Run:  flyctl auth login"
fi
ok "Signed in to Fly as $(flyctl auth whoami 2>/dev/null)"

[ -f .env ] || die "No .env here. The deploy reads your keys from it.
    cp .env.example .env   and fill it in first."

# --- 2. The app -------------------------------------------------------- #
APP="${1:-}"
if [ -z "$APP" ]; then
    APP="$(grep -E '^app[[:space:]]*=' fly.toml | head -1 | cut -d'"' -f2 || true)"
fi
[ -n "$APP" ] || die "No app name. Pass one:  bash scripts/deploy-fly.sh my-desk-name"

if flyctl status --app "$APP" >/dev/null 2>&1; then
    ok "App $APP already exists"
else
    say "${DIM}Creating app $APP...${OFF}"
    # --no-deploy because secrets and the volume must exist first; a
    # first boot without them would crash-loop and look like a build bug.
    flyctl apps create "$APP" || die "Could not create $APP. The name may be
    taken — Fly app names are global. Try another:
    bash scripts/deploy-fly.sh something-more-unique"
    ok "Created $APP"
fi

# fly.toml carries the app name on every later command.
if ! grep -q "^app = \"$APP\"" fly.toml; then
    say "${DIM}Pointing fly.toml at $APP...${OFF}"
    # A temp file rather than sed -i, which differs between macOS and GNU.
    awk -v app="$APP" '/^app[[:space:]]*=/ { print "app = \"" app "\""; next } { print }' \
        fly.toml > fly.toml.tmp && mv fly.toml.tmp fly.toml
fi

# --- 3. The volume ----------------------------------------------------- #
# The trade journal lives here. Without it every deploy restarts the
# journal, the equity curve and the day's opening equity from empty — and
# the daily-loss circuit breaker measures against that opening equity.
if flyctl volumes list --app "$APP" 2>/dev/null | grep -q "$VOLUME"; then
    ok "Volume $VOLUME already exists"
else
    say "${DIM}Creating the $VOLUME volume in $REGION...${OFF}"
    # `volumes create` grew --yes at some point; older flyctl (0.5.4 and
    # around) rejects it as an unknown flag, and without it the single-
    # volume redundancy warning waits on stdin forever. Use the flag when
    # this flyctl has it, and answer the prompt when it does not.
    VOL_YES=""
    if flyctl volumes create --help 2>&1 | grep -q -- '--yes'; then
        VOL_YES="--yes"
    fi
    printf 'y\n' | flyctl volumes create "$VOLUME" --app "$APP" \
        --region "$REGION" --size 1 $VOL_YES \
        || die "Could not create the volume."
    ok "Created volume $VOLUME (1GB, $REGION)"
fi

# --- 4. Secrets -------------------------------------------------------- #
# Everything from .env except the keys fly.toml already sets, plus a
# dashboard password if there is not one. Written to a temp file with
# tight permissions and deleted on the way out, including on failure.
STAGED="$(mktemp -t trader-secrets.XXXXXX)"
chmod 600 "$STAGED"
cleanup() { rm -f "$STAGED" fly.toml.tmp 2>/dev/null || true; }
trap cleanup EXIT INT TERM

grep -E '^[A-Z][A-Z0-9_]*=' .env \
  | grep -v -E '^(DATA_DIR|LOG_DIR|WEBHOOK_HOST|WEBHOOK_PORT|AUTOSTART_SCANNER)=' \
  | grep -v -E '^[A-Z0-9_]*=[[:space:]]*$' \
  > "$STAGED" || true

GENERATED=""
if ! grep -q '^DASHBOARD_PASSWORD=.\+' "$STAGED"; then
    GENERATED="$(LC_ALL=C tr -dc 'A-Za-z0-9' < /dev/urandom | head -c 28)"
    printf 'DASHBOARD_PASSWORD=%s\n' "$GENERATED" >> "$STAGED"
fi

COUNT="$(grep -c '=' "$STAGED" || true)"
say "${DIM}Sending $COUNT settings to Fly as secrets...${OFF}"
flyctl secrets import --app "$APP" --stage < "$STAGED" >/dev/null \
    || die "Could not set secrets."
ok "Secrets stored (values are not printed anywhere)"

# --- 5. Deploy --------------------------------------------------------- #
say ""
say "${DIM}Building and deploying. First run takes a few minutes...${OFF}"
say ""
flyctl deploy --app "$APP" --ha=false || die "Deploy failed. The build log is above.
    Logs from the running app:  flyctl logs --app $APP"

# --ha=false keeps it to ONE machine. Two means two schedulers: double
# scanning, double the Claude bill, and in auto mode duplicate orders.

# --- 6. Check it is actually up ---------------------------------------- #
URL="https://$APP.fly.dev"
say ""
say "${DIM}Waiting for it to answer...${OFF}"
UP=""
for _ in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20; do
    if curl -fsS -m 5 "$URL/api/live" >/dev/null 2>&1; then UP="yes"; break; fi
    sleep 3
done

say ""
if [ -n "$UP" ]; then
    ok "Deployed and answering"
else
    warn "Deployed, but /api/live has not answered yet."
    say  "  Check:  flyctl logs --app $APP"
fi

say ""
say "  Your bot:  ${GREEN}${URL}${OFF}"
say "  Username:  ${GREEN}trader${OFF}"
if [ -n "$GENERATED" ]; then
    say ""
    warn "Dashboard password (shown ONCE — save it now):"
    say  "  ${GREEN}${GENERATED}${OFF}"
    say  "  ${DIM}Lost it? Set a new one:${OFF}"
    say  "  ${DIM}flyctl secrets set DASHBOARD_PASSWORD=\"\$(openssl rand -base64 24)\" --app $APP${OFF}"
else
    say "  Password:  the DASHBOARD_PASSWORD from your .env"
fi
say ""
say "  ${DIM}Logs:    flyctl logs --app $APP${OFF}"
say "  ${DIM}Status:  flyctl status --app $APP${OFF}"
say "  ${DIM}Stop:    flyctl scale count 0 --app $APP${OFF}"
say ""
warn "It starts scanning on boot (AUTOSTART_SCANNER=true in fly.toml)."
say  "  Check the mode on the dashboard before you fund anything real."
say  ""
