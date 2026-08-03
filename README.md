# AI Trading Bot

An AI-assisted trading system: it pulls in market data and news, sends them to
Claude for analysis, generates a trade idea **with a risk plan**, and — only if
you approve — routes it to a broker while alerting you on Discord or Telegram.

An always-on analyst that never sleeps, drafts the trade, and hands it to you.
You stay the trader.

> **Read this first — real-money risk.**
> Trading involves real risk of loss. AI models make mistakes and can be
> confidently wrong. Nothing here is financial advice. Never risk money you
> can't afford to lose. Start in paper mode and stay there until the system has
> proven itself over weeks. This is a tool, not a money printer.

---

## Architecture

```
Market Data ──► Indicators ──► News API ──► Claude ──► Signal Generation
                                                              │
                                                              ▼
                                                     Risk Management
                                                              │
                                        ┌─────────────────────┴──────────┐
                                        │                                │
                                   alert mode                       auto mode
                                        │                                │
                                 you approve  ──────────────►  Broker API (Alpaca)
                                        │                                │
                                        └──────────► Trade Execution ◄───┘
                                                              │
                                                    Journal ──┴──► Discord / Telegram
```

**The risk layer sits between the AI and the broker on purpose. The AI never
talks straight to your money — the risk rules are the gate.**

---

## What's in the box

| Piece | Where | What it does |
|---|---|---|
| Market data | `src/data/market_data.py` | Alpaca bars, with a CSV provider for offline work |
| Indicators | `src/data/indicators.py` | SMA/EMA/RSI/ATR/MACD/Bollinger, pivots, trend |
| News | `src/data/news.py` | NewsAPI headlines, aggressively cached |
| Prompts | `src/ai/prompts.py` | The ten prompts, verbatim, plus JSON schemas |
| Analyst | `src/ai/analyst.py` | Chart + news → a setup, or explicitly no trade |
| Agent debate | `src/ai/debate.py` | Bull, bear and a risk manager who decides |
| Strategies | `strategies/` | Mechanical rules that pre-filter before any model call |
| Risk gate | `src/risk.py` | Hard limits — every rule is a reject, not a score |
| Sizing | `src/sizing.py` | Size from stop distance, never gut feel |
| Approvals | `src/approvals.py` | Human-in-the-loop queue, with expiry |
| Execution | `src/execution.py` | Bracket orders; the only code that spends money |
| Journal | `src/journal.py` | SQLite record of signals, orders, trades, equity |
| Metrics | `src/metrics.py` | Win rate, avg R, expectancy, profit factor, max DD |
| Backtester | `src/backtest.py` | Replays the rules, with lookahead guards |
| Dashboard | `src/webhook_server.py` | Live P&L, positions, approve/deny, TradingView hook |
| Scheduler | `src/scheduler.py` | The 15-minute loop and the daily report |

---

## Quick start

### 1. Install

```bash
python -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate
pip install -r requirements-trader.txt
```

Python 3.11+ is required.

### 2. Configure

```bash
cp .env.example .env
```

Fill in your keys. `.env` is gitignored — **never commit it.** Leaked broker
keys can drain a real account.

| Service | Why | Where |
|---|---|---|
| Anthropic | The analysis brain | console.anthropic.com |
| Alpaca | Paper/live trades + market data | alpaca.markets → **use PAPER keys first** |
| NewsAPI | Headlines for context | newsapi.org |
| Discord | Trade alerts | Server Settings → Integrations → Webhook |
| Telegram | Trade alerts + approve/deny buttons | @BotFather → new bot → token |
| OpenRouter | Optional: one key, many models | openrouter.ai |

Set up a dedicated email and a fresh broker paper account just for this. It
keeps your keys, alerts and experiments separate from anything real.

### 3. Check the setup

```bash
python -m src.cli doctor              # offline checks
python -m src.cli doctor --check-apis # also makes one real call to each API
```

`doctor` catches the things that actually go wrong: a missing `.env`, keys with
trailing spaces, a `.env` that isn't gitignored, missing packages, and whether
live trading is (correctly) still switched off.

### 4. Run it

```bash
python -m src.cli scan     # one pass over the watchlist
python -m src.cli pending  # what's waiting on your approval
python -m src.cli approve sig_abc123
python -m src.cli run      # the scheduled loop
python -m src.cli serve    # dashboard on http://localhost:8001
```

### No keys yet? Run the whole thing offline

```bash
python scripts/make_sample_data.py
python -m src.cli backtest --symbols AAPL,MSFT --bars 400
python -m src.cli scan --simulated
```

`--simulated` uses a local broker and CSV prices, so every stage runs without a
single API key. The sample data is **random** — it proves the wiring works and
says nothing whatsoever about any strategy.

---

## Commands

| Command | What it does |
|---|---|
| `doctor` | Check the setup and diagnose problems |
| `scan` | One pass over the watchlist |
| `run` | The scheduled scan loop + daily report |
| `serve` | Dashboard and TradingView webhook |
| `backtest` | Replay the mechanical rules on past data |
| `analyze SYMBOL` | One-off AI market analysis and chart breakdown |
| `pending` / `approve` / `deny` | The human gate |
| `positions` / `close SYMBOL` | Account state and manual exits |
| `metrics` | Performance from the journal |
| `review` | AI review of closed trades — what you did wrong |
| `optimize STRATEGY` | AI review of a strategy's history |
| `portfolio-review` | AI read on concentration and correlation |
| `export` | Closed trades to CSV |
| `reset-drawdown` | Clear a drawdown pause, after you've reviewed it |

Useful flags: `--debate` (bull/bear/risk-manager, 3× the model calls),
`--always-analyse` (send every symbol to the AI, not just strategy triggers),
`--simulated` (fully offline).

---

## Risk management

This is the most important part. A mediocre strategy with great risk control
survives; a great strategy with no risk control blows up.

| Rule | Default | Setting |
|---|---|---|
| Risk per trade | ≤ 1% of account | `RISK_PER_TRADE_PCT` |
| Stop loss | Mandatory, set before entry | enforced in code |
| Daily loss limit | Stop trading at −3% | `DAILY_LOSS_LIMIT_PCT` |
| Max drawdown | Pause + review at −15% | `MAX_DRAWDOWN_PCT` |
| Position sizing | From stop distance | `src/sizing.py` |
| Correlated positions | Max 2 per group | `MAX_CORRELATED_POSITIONS` |
| Open positions | Max 5 | `MAX_OPEN_POSITIONS` |
| Total open risk | Max 6% | `MAX_PORTFOLIO_RISK_PCT` |
| Minimum R:R | 1.5 | `MIN_RISK_REWARD` |

A signal that fails **any** of these is rejected, logged with the reason, and
never reaches the broker. Rejections are journalled too — the daily report
tells you what got blocked and why, which is often more informative than the
trades that got through.

The drawdown pause survives restarts. Clearing it is a deliberate act
(`reset-drawdown`), because the point of the pause is that you review the
system before trading it again.

### Sizing, concretely

```
risk_dollars = equity × RISK_PER_TRADE_PCT / 100
shares       = risk_dollars / |entry − stop|
```

A wide stop gives a small position, a tight stop a larger one, and the dollar
loss if you're wrong is the same either way. Three caps sit on top (max
position size, buying power, whole shares for equities) and the alert tells you
which one bound.

> **Note the interaction:** a full-risk position needs notional of
> `risk% / stop-distance%`. At 1% risk, a 2% stop wants a position worth 50% of
> equity. If `MAX_POSITION_PCT` is tighter than that, tight-stop trades get
> trimmed and risk less than your configured 1%. That's safe, but it isn't what
> you set — the bot logs the threshold at startup and shows `capped_by` on every
> alert.

### The AI can be confidently wrong

The model will sometimes hand you a clean-looking setup that's garbage, and it
has no idea. That's why `EXECUTION_MODE=alert` is the default: signals become
*pending approvals*, not orders. You see the reasoning and the invalidation
levels, and you decide.

Approvals expire (`APPROVAL_TTL_MINUTES`, default 45). A setup that was clean
forty minutes ago usually isn't, and an approval sitting in a chat overnight is
a trap.

---

## Testing before going live

| Stage | What it proves | How |
|---|---|---|
| Backtesting | How the rules would have done on past data | `python -m src.cli backtest` |
| Paper trading | How it behaves live, with fake money | Alpaca paper keys |
| Forward testing | Real-time results over weeks, still on paper | `python -m src.cli run` |
| Metrics | Win rate, avg R, max drawdown, profit factor | `python -m src.cli metrics` |

The backtester tests the **mechanical rules only** — not the AI. You cannot
reproduce what a model would have said six months ago, and pretending otherwise
produces a number that means nothing. So the rules get backtested and the AI
layer gets forward-tested on paper.

It also guards the two classic ways a backtest lies to you: decisions use only
bars up to that point and fill at the *next* bar's open, and when a bar contains
both the stop and the target it assumes the stop hit first.

**Rule of thumb: paper-trade for at least a few weeks and 30+ trades before you
even think about live — then start with tiny size.** `metrics` and the daily
report both refuse to draw conclusions below 30 trades and say so explicitly.

If backtest results look too good, they're curve-fitted. Re-run on data the
rules weren't designed against.

---

## Going live

Live trading needs **three independent switches** to agree:

```bash
TRADING_MODE=live
ALPACA_PAPER=false
CONFIRM_LIVE=I_UNDERSTAND_THE_RISK
```

Flipping one by accident does nothing. `doctor` shows exactly which blockers
are still in place. Even then, `EXECUTION_MODE=alert` means nothing is ordered
without you tapping approve — full auto-execution is an advanced, higher-risk
mode. Earn it.

---

## TradingView integration

Point a TradingView alert webhook at `http://your-host:8001/webhook/tradingview`
with this message body:

```json
{"secret": "<TRADINGVIEW_WEBHOOK_SECRET>", "symbol": "{{ticker}}"}
```

A matching alert triggers analysis of that symbol. The endpoint is public by
nature, so the secret is mandatory, only watchlisted symbols are accepted, and
an alert can never place an order — it can only ask the bot to look.

## Telegram approve/deny from your phone

Set `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`, expose the server, and point
Telegram at it:

```python
from src.alerts.telegram import TelegramAlerts
TelegramAlerts(token, chat_id).set_webhook("https://your-host/webhook/telegram")
```

Signals then arrive with Approve / Deny buttons. Only your configured chat can
act on them.

---

## Advanced upgrades

Add one at a time and re-test after each — complexity is where bots quietly
break.

- **Multi-agent debate** (`--debate`) — a bull, a bear, and a risk manager who
  decides. A setup has to survive an adversary before it becomes a signal.
- **Portfolio dashboard** — `serve`, then open `http://localhost:8001`.
- **Mobile approve/deny** — Telegram buttons, above.
- **Multi-broker routing** — `BrokerRouter` routes by asset class.
- **Sentiment / news as an input** — on by default when `NEWS_API_KEY` is set.
- **Crypto** — put Alpaca-style pairs in `CRYPTO_WATCHLIST` (e.g. `BTC/USD`).
  Note Alpaca has no bracket orders for crypto, so the stop is tracked bot-side
  only; the bot warns when this applies.
- **AI journaling** — `review` and `optimize` read your closed trades back to
  you and name the recurring mistake.

---

## Troubleshooting

| Problem | Fix |
|---|---|
| API errors / 401 | Wrong or expired key; check `.env` for trailing spaces. Run `doctor`. |
| Rate limits / 429 | Calling too fast — raise `SCAN_INTERVAL_MINUTES` or `NEWS_CACHE_TTL`, shorten the watchlist. |
| No signals firing | Filters too strict, or the market is flat. Try `--always-analyse` to bypass the mechanical pre-filter. |
| Install fails | Wrong Python version; use 3.11+ in a fresh venv. |
| Orders rejected (422) | Paper account not enabled, bad symbol format, or the market is closed. |
| Strategy bug | Everything is logged to `logs/bot.jsonl`; test one piece at a time. |
| Fable 5 returns 400 | It requires 30-day data retention on your org. Set `CLAUDE_MODEL=claude-opus-5`. |
| Going live too fast | Back to paper. |

When something breaks, paste the exact error into Claude along with the
relevant code. It's the fastest debugger you have.

---

## Tests

```bash
pip install pytest
python -m pytest tests/ -q
```

124 tests, no network calls. They cover the parts where being wrong costs
money: sizing arithmetic, every risk rule, the approval gate (including that
alert mode really does order nothing and that a double-tap can't double-order),
journal R-multiples, backtest lookahead guards, and the full pipeline
end-to-end against a stubbed model.

---

## Also in this repo

`agents/`, `services/`, `backend/` and `frontend/` hold a separate
**prediction-market bot** (Polymarket/Kalshi) from earlier work — see
[docs/prediction-market-bot.md](docs/prediction-market-bot.md). It's an
independent system with its own dependencies in `requirements.txt`, unrelated
to the trading bot documented above.

---

## Disclaimer

Not financial advice. Trade at your own risk. Backtest results are not
predictions. The AI is an analyst, not an oracle, and you are the final check
on every signal it produces.
