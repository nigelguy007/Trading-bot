#!/usr/bin/env bash
#
# Scanner A — Premarket Gappers (reference implementation)
# ---------------------------------------------------------------------------
# This is a standalone reference version of the scanner the video has Claude
# generate at runtime. In the real workflow you paste prompts/05_scanner_a_*.txt
# into Claude Code and Claude writes/maintains this script for you, using its
# WebFetch tool for the news catalysts. This version uses plain curl + python so
# it can run without Claude, but note that Yahoo/Benzinga HTML changes often —
# if parsing breaks, have Claude regenerate it with the Step 5 prompt.
#
# Output: ./premarket_gappers_YYYY-MM-DD.json
#
# Filters (edit to taste):
#   GAP_MIN   minimum gap %          (default 5)
#   PRICE_MIN minimum share price    (default 3)
#   VOL_MIN   minimum premarket vol  (default 50000)
#   TOP_N     keep top N by gap desc (default 10)
# ---------------------------------------------------------------------------
set -euo pipefail

GAP_MIN="${GAP_MIN:-5}"
PRICE_MIN="${PRICE_MIN:-3}"
VOL_MIN="${VOL_MIN:-50000}"
TOP_N="${TOP_N:-10}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUT="${SCRIPT_DIR}/premarket_gappers_$(date +%F).json"

GAINERS_URL="https://query1.finance.yahoo.com/v1/finance/screener/predefined/saved?scrIds=day_gainers&count=100"

echo "[scanner-a] fetching gainers…" >&2
RAW_FILE="$(mktemp)"
trap 'rm -f "$RAW_FILE"' EXIT
curl -sS -A 'Mozilla/5.0' "$GAINERS_URL" -o "$RAW_FILE" || true

python3 - "$RAW_FILE" "$GAP_MIN" "$PRICE_MIN" "$VOL_MIN" "$TOP_N" "$OUT" <<'PY'
import json, sys, datetime

raw_file, gap_min, price_min, vol_min, top_n, out = sys.argv[1:7]
gap_min, price_min, vol_min, top_n = float(gap_min), float(price_min), float(vol_min), int(top_n)

rows = []
try:
    with open(raw_file) as f:
        data = json.load(f)
    quotes = data["finance"]["result"][0]["quotes"]
except Exception:
    quotes = []

for q in quotes:
    sym  = q.get("symbol")
    price = q.get("regularMarketPrice") or q.get("preMarketPrice")
    gap   = q.get("preMarketChangePercent")
    if gap is None:
        gap = q.get("regularMarketChangePercent")
    vol   = q.get("regularMarketVolume") or 0
    if not sym or price is None or gap is None:
        continue
    if gap > gap_min and price > price_min and vol > vol_min:
        rows.append({"symbol": sym, "price": round(float(price), 2),
                     "gap_pct": round(float(gap), 2), "premarket_volume": int(vol)})

rows.sort(key=lambda r: r["gap_pct"], reverse=True)
rows = rows[:top_n]

gappers = []
for i, r in enumerate(rows, 1):
    gappers.append({
        "rank": i, "symbol": r["symbol"], "price": r["price"],
        "gap_pct": r["gap_pct"], "premarket_volume": r["premarket_volume"],
        # In the Claude workflow these are filled by WebFetching benzinga.com/quote/{TICKER}.
        # Left null here so a single failed lookup never aborts the scan.
        "catalyst": None, "headlines": [],
    })

payload = {"scanned_at": datetime.datetime.now().astimezone().isoformat(),
           "gappers": gappers}
with open(out, "w") as f:
    json.dump(payload, f, indent=2)

if gappers:
    top = ", ".join(f"{g['symbol']} ({g['gap_pct']}%)" for g in gappers[:3])
    print(f"Premarket Gappers: {len(gappers)} names. Top: {top}")
else:
    print("Premarket Gappers: 0 names passed the filters.")
PY

echo "[scanner-a] wrote ${OUT}" >&2
