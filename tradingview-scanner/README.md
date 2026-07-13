# TradingView + Claude AI Day-Trading Scanner Bot

A step-by-step build of the automated day-trading scanner from the video
**"SmallTrading"** (Humbled Trader). It wires **Claude Code** to **TradingView
Desktop** through the **TradingView MCP**, so Claude can read your live charts,
scan for setups, backtest a strategy in PineScript, and text you the hits on
Telegram.

> Source article the video follows:
> https://www.humbledtrader.com/blog/tradingview-claude-ai-scanner/

## What it does

1. **Scanner A — Premarket Gappers.** Every morning pulls the biggest premarket
   movers, filters junk, and attaches the news catalyst behind each gap.
2. **Scanner B — Trend Join Long.** Checks which gappers actually meet your
   intraday entry setup *right now* (reads live charts via the MCP).
3. **Backtest.** Injects a PineScript strategy into TradingView, compiles it, and
   reads the Strategy Tester results back.
4. **Alerts.** Sends both scanners' results to your phone via Telegram.
5. **Automation.** Schedules everything with macOS `launchd`.

## ⚠️ Read first — how this actually works

The "bot" is **generated and run by Claude Code on your own Mac**, talking to
**TradingView Desktop** over a local debug port. The real deliverable is the set
of **prompts you paste into Claude Code** (`prompts/`). Claude writes and
maintains the scripts for you.

This folder gives you:
- The **exact prompts** for every step (`prompts/01`–`prompts/11`) — the source of truth.
- **Reference implementations** (`scanners/`, `pine/`, `lib/`, `scheduling/`) so you
  have working code even before Claude regenerates it, and something to diff against.

Requirements that can **only** run on your machine (not in this repo/CI):
TradingView **Desktop** (browser version does NOT work with the MCP), a paid
Claude plan for Claude Code, macOS for the `launchd` scheduling. The MCP runs
locally — your TradingView credentials are never shared with Claude/Anthropic.

## Folder layout

```
tradingview-scanner/
├── prompts/       # paste-into-Claude prompts for every step (verbatim)
├── scanners/      # premarket_gappers.sh  (Scanner A reference)
├── pine/          # trend_join_breakout.pine  (TJL strategy)
├── lib/           # telegram_notify.sh
├── scheduling/    # launchd plist template for Scanner A
├── .env.example   # Telegram token + filter knobs (copy to .env)
└── README.md
```

---

## Step-by-step

### Step 1 — Install Claude Code
Terminal (Mac):
```bash
curl -fsSL https://claude.ai/install.sh | bash
claude --version    # should print a version
```
Sign in with a **paid** Claude plan (no free tier for Claude Code). Pro ($20) is
enough to learn; Max 5x ($100) if you'll run Scanner B 9×/day in production.

### Step 2 — Install TradingView Desktop
Pick any paid plan (Essential is enough for scanning; Pro+ for PineScript bar
replay). **Download the Desktop app** — the browser version will not work with
the MCP. Sign in and pin a starter chart.

### Step 3 — Install the TradingView MCP
The bridge that lets Claude read your live charts (built by **@Tradesdontlie**,
repo: https://github.com/tradesdontlie/tradingview-mcp). Two prompts with a
restart in between — see **`prompts/03_install_tradingview_mcp.txt`**.
Success = `cdp_connected: true` and `api_available: true`.

> TradingView Desktop must be running with a **real chart tab open** any time
> you use the scanners. The "New Tab" welcome screen doesn't count.

### Step 4 — Try a few basic prompts
Warm up with `prompts/04_basic_prompts.txt` (change symbol, add indicators, read
watchlist, draw levels, EOD briefing).

### Step 5 — Build Scanner A (premarket gappers)
Paste `prompts/05_scanner_a_premarket_gappers.txt`. Claude writes a shell script
that fetches gainers, filters (`gap>5%`, `price>$3`, `vol>50k`, top 10) and pulls
a news catalyst per name from Benzinga. Output: `premarket_gappers_YYYY-MM-DD.json`.
Reference version: `scanners/premarket_gappers.sh`.

### Step 6 — Automate Scanner A
Paste `prompts/06_automate_scanner_a.txt` → Claude sets up a `launchd` job at
8:30am ET with a catch-up layer for when the laptop was asleep. Verify:
`launchctl list | grep -i premarket`. Template: `scheduling/com.tjl.premarket-scanner.plist`.

### Step 7 — Build Scanner B (Trend Join Long)
Paste `prompts/07_scanner_b_strategy.txt`. For each ticker Claude reads the daily
+ 1-min charts via the MCP and checks: **PASS** if
`price > prev daily high AND prev daily close > SMA200` (daily) **and**
`price > premarket high AND price > today's high-of-day` (intraday). Output:
`tjl_watchlist_YYYY-MM-DD_HHMMET.json`. (Slow — 3-5 min/ticker; that's why you automate it.)

### Step 8 — Automate Scanner B every 30 min
Paste `prompts/08_automate_scanner_b.txt` → fires 10am–2pm ET, only pings you on
first run of day or a **new** hit.

### Step 9 — Backtest with PineScript
Paste `prompts/09_backtest_pinescript.txt` and append the contents of
`pine/trend_join_breakout.pine`. Claude opens the "Demo TJL Strategy" Pine slot,
injects + compiles the code (expect 2-3 compile cycles), applies it, and reads
win rate / trades / P&L / profit factor off the Strategy Tester.
Video's own result (MU 5-min, ~60 days): 21 trades, 66.67% win, +$49.24, PF 1.57.

### Step 10 — Test an "obvious" filter (and learn from it)
Paste `prompts/10_test_regime_filter.txt` to add an SPY/QQQ regime filter and
compare. In the video it **hurt** the strategy (PF 1.59 → 1.28). Lesson: test
everything, even the obvious stuff.

### Step 11 — Telegram alerts
Create a bot with @BotFather, get your token + chat ID, put them in `.env`
(copy from `.env.example`), then paste `prompts/11_telegram_alerts.txt`. Helper:
`lib/telegram_notify.sh`. **Never commit your token.**

### Step 12 — Paper trading (preview)
The video's next installment connects to Interactive Brokers **paper** for
auto-execution. 🚨 Don't point any of this at a live account until you've paper
-traded it for at least a month.

---

## Quick start (reference scripts, no Claude)
```bash
cd tradingview-scanner
cp .env.example .env          # add your Telegram token + chat id
bash scanners/premarket_gappers.sh   # writes premarket_gappers_<date>.json
```
Scanner B and the backtest need TradingView Desktop + the MCP + Claude Code —
follow Steps 3, 7, and 9.

## Cost & notes
- Learn on Claude **Pro ($20)** + TradingView **Essential**.
- Full automation (Scanner B 9×/day): TradingView Essential + Claude **Max 5x
  ($100)** ≈ ~$113/mo.
- The MCP uses TradingView's internal debug interface (not an official API) — pin
  your Desktop version once it works. Reading your own paid charts for personal
  use is fine; redistributing/reselling the data is not.

*Not financial advice. Backtest results are not future results.*
