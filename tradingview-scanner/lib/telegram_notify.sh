#!/usr/bin/env bash
# Telegram notify helper — source this or call it directly.
#   send_telegram "your *markdown* message"
# Reads TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID from the environment (.env).
# Never echoes the token. On failure it logs and returns non-zero WITHOUT
# aborting the calling scanner (callers should `|| true`).
set -uo pipefail

_load_env() {
  local dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
  [ -f "$dir/.env" ] && set -a && . "$dir/.env" && set +a || true
}

send_telegram() {
  local text="$1"
  _load_env
  if [ -z "${TELEGRAM_BOT_TOKEN:-}" ] || [ -z "${TELEGRAM_CHAT_ID:-}" ]; then
    echo "[telegram] missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID — skipping" >&2
    return 1
  fi
  local http
  http=$(curl -s -o /dev/null -w '%{http_code}' \
    "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
    -d chat_id="${TELEGRAM_CHAT_ID}" \
    --data-urlencode text="${text}" \
    -d parse_mode=Markdown) || { echo "[telegram] curl failed" >&2; return 1; }
  if [ "$http" != "200" ]; then
    echo "[telegram] send failed (HTTP $http)" >&2
    return 1
  fi
}

# Allow direct CLI use: lib/telegram_notify.sh "message"
if [ "${BASH_SOURCE[0]}" = "${0}" ] && [ "$#" -ge 1 ]; then
  send_telegram "$1"
fi
