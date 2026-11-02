# Integration Handoff — AI Trading Bot

**Source:** `nigelguy007/Trading-bot`, branch `claude/bot-build-r5rq2r` (HEAD `e5822e9`)
**Language:** Python 3.11+ · **Lines:** ~5,500 source, ~1,800 test · **Tests:** 142 passing, no network
**Purpose of this doc:** take parts of this bot into another trading platform without re-deriving the design.

---

## 1. What this is

A pipeline that turns market data into an approved, risk-sized order:

```
market data ─► indicators ─► news ─► Claude ─► signal ─► RISK GATE ─► approval ─► broker
                                                            │
                                              journal ◄─────┴─────► alerts
```

The load-bearing idea: **the risk gate sits between the model and the broker.** The AI
proposes; a deterministic rule set decides. Nothing reaches a broker without passing
`RiskManager.evaluate()`, and in the default mode nothing reaches a broker without a
human tapping approve.

If you take one thing, take the gate (§4).

---

## 2. Portability map

The modules are deliberately layered by how much they know about the outside world.
This is the table to plan your integration from.

| Module | LOC | External coupling | Portable? |
|---|---|---|---|
| `src/sizing.py` | 60 | none (pure arithmetic) | **Drop-in** |
| `src/metrics.py` | 167 | none (dicts in, dataclass out) | **Drop-in** |
| `src/risk.py` | 179 | `Settings`, `PortfolioState`, `Signal` | **Drop-in** w/ 3 shims |
| `src/data/indicators.py` | 190 | pandas/numpy only | **Drop-in** |
| `src/models.py` | 217 | none (dataclasses) | **Drop-in** |
| `src/ai/prompts.py` | 220 | none (strings + JSON schemas) | **Drop-in** |
| `src/ai/client.py` | 286 | Anthropic SDK / OpenRouter | Portable, swap transport |
| `src/ai/analyst.py` | 245 | `LLMClient`, `prompts`, `models` | Portable |
| `src/ai/debate.py` | 100 | `LLMClient`, `Analyst` | Portable |
| `src/journal.py` | 325 | SQLite (stdlib) | Portable, or re-point to your DB |
| `src/approvals.py` | 126 | JSON file on disk | Portable, or re-point to your DB |
| `src/backtest.py` | 279 | `MarketDataProvider`, strategies | Portable |
| `strategies/` | 170 | `TechnicalSnapshot` | **Drop-in** |
| `src/broker/base.py` | 40 | — (ABC) | Implement against your broker |
| `src/broker/alpaca.py` | 190 | Alpaca REST | Reference impl — likely discard |
| `src/broker/ig.py` | 600 | IG REST | Reference impl for a **geared** broker — read it if your platform trades derivatives |
| `src/data/market_data.py` | 330 | Alpaca / IG REST | Reference impl — likely discard |
| `src/execution.py` | 161 | `Broker`, journal, approvals, alerts | Rewire to your order path |
| `src/pipeline.py` | 228 | everything | Read as a spec, not as code to port |
| `src/alerts/` | 335 | Discord/Telegram HTTP | Swap for your notifier |
| `src/webhook_server.py` | 396 | FastAPI | Optional |
| `src/scheduler.py` | 118 | APScheduler | Optional — your platform likely has a loop |

**Deliberate design property:** nothing in `risk.py`, `sizing.py`, `metrics.py`,
`indicators.py` or `strategies/` imports a broker, an HTTP client, or the LLM. Those
five are the clean core and will lift without modification.

---

## 3. Three integration levels

Pick one. Level 1 is a half-day; Level 3 is a week.

### Level 1 — Take the risk gate only (recommended first step)

Copy `src/sizing.py`, `src/risk.py`, `src/models.py`, `src/metrics.py`. Implement
three adapters mapping your platform's types to these:

```python
# 1. Your account state -> Account
Account(equity=..., cash=..., buying_power=..., last_equity=...)

# 2. Your positions -> list[Position]
Position(symbol=..., qty=..., avg_entry=..., market_price=..., unrealized_pnl=...)

# 3. Your trade idea -> Signal  (must carry a stop; that is the point)
Signal(symbol=..., direction="long"|"short", entry=..., stop=..., target=...,
       confidence=0.0..1.0, reasoning=...)
```

Then, per candidate trade:

```python
state    = PortfolioState(account=..., positions=..., open_risk=..., peak_equity=...,
                          drawdown_pct=..., day_pnl_pct=...)
breaker  = risk_manager.check_circuit_breakers(state)   # once per cycle, before anything
decision = risk_manager.evaluate(signal, state)         # per signal

if decision.approved:
    place_order(signal, decision.sizing)   # sizing.shares, sizing.dollar_risk
else:
    log(decision.reason)                   # always record why, not just that
```

`RiskManager.__init__` needs a `portfolio` object, but only calls one method on it:
`group_for(symbol) -> str` (the correlation bucket). A dict lookup satisfies it —
see `DEFAULT_CORRELATION_GROUPS` in `src/portfolio.py`.

### Level 2 — Take the AI analysis layer as well

Add `src/ai/` and `src/data/indicators.py`. Feed `build_snapshot(symbol, candles)`
with your own candles (any source — it takes `list[Candle]`, a 6-field dataclass),
then `Analyst.find_setup(snapshot, news)` returns a validated `Signal` or `None`.

The prompts in `src/ai/prompts.py` are the ten from the source guide, unchanged, plus
JSON schemas for structured output. They are plain strings; reuse them with any model.

### Level 3 — Run this as a signal service beside your platform

Keep the repo intact, run `python -m src.cli serve`, and consume:

- `GET /api/pending` → signals awaiting approval (JSON)
- `POST /api/signals/{id}/approve` → places the order through this bot's broker
- `GET /api/portfolio`, `/api/metrics`, `/api/trades`, `/api/signals`

Your platform becomes the broker by implementing `src/broker/base.py` (§5) and
registering it in `src/broker/registry.py`. This keeps the risk gate and journal here
and gives your platform execution only.

---

## 4. The risk rules (what you are actually buying)

All in `src/risk.py`. Every one is a hard reject, not a weighted score — that is the
whole reason they are in code rather than in someone's judgement at 3pm.

**Portfolio-level, checked once per cycle before any work:**

| Rule | Default | Behaviour |
|---|---|---|
| Max drawdown | −15% from peak equity | Halt; survives restart; manual clear only |
| Daily loss limit | −3% | Halt for the day |

**Per signal:**

| Rule | Default |
|---|---|
| Stop must exist and be on the correct side of entry | enforced |
| Risk per trade | ≤ 1% of equity |
| Minimum reward:risk | 1.5 |
| Minimum model confidence | 0.6 |
| No adding to an existing position | enforced |
| Max open positions | 5 |
| Max correlated positions per group | 2 |
| Max total open risk | 6% of equity |

**Sizing (`src/sizing.py`):**

```
risk_dollars = equity × risk_per_trade_pct / 100
shares       = risk_dollars / |entry − stop|
```

Then three caps, with whichever bound recorded in `sizing.capped_by`: max position
notional, buying power, and whole-share rounding for equities (crypto sizes
fractionally).

> **Carry this caveat across.** A full-risk position needs notional of
> `risk% / stop-distance%`. At 1% risk a 2% stop wants a position worth 50% of equity.
> If your platform's position cap is tighter than that, tight-stop trades get trimmed
> and silently risk *less* than configured. Safe, but not what the config says. We set
> `MAX_POSITION_PCT=50` for exactly this reason and log the threshold at startup —
> `Settings.implied_min_stop_pct` computes where it bites. This was a real bug caught
> by tests, not a hypothetical.

**Drawdown persistence:** the equity high-water mark lives in
`data/portfolio_state.json`, so restarting does not clear a drawdown pause. If you
port the gate, give the high-water mark a durable home in your platform too, or the
−15% rule becomes decorative.

---

## 5. Interfaces to implement

### Broker (`src/broker/base.py`)

```python
class Broker(ABC):
    name: str
    @property
    def is_paper(self) -> bool: ...
    def get_account(self) -> Account: ...
    def get_positions(self) -> list[Position]: ...
    def submit_bracket_order(self, signal: Signal, sizing: PositionSizing) -> OrderResult: ...
    def close_position(self, symbol: str) -> OrderResult: ...
    def is_market_open(self) -> bool: ...          # default True
    def supports(self, asset_class: str) -> bool:  # default equity+crypto
    def close(self) -> None: ...
```

`submit_bracket_order` is named that way on purpose: **entry, stop and target go out
together** so the stop exists at the broker from the moment of fill, not whenever the
bot next wakes up. If your platform cannot submit brackets atomically, that is a
behaviour change you must consciously accept and compensate for — see the crypto gap
in §8.

`PositionSizing.unit` says what `shares` means: `"shares"` for a share account,
`"GBP/point"` for a spread bet. If your platform trades anything geared, carry that
field through to every display — a stake per point rendered as a share count reads as
a position several times smaller than it is.

Three reference implementations: `alpaca.py` (real REST, bracket orders, mapped error
messages) and `paper.py` (local simulator, JSON state, used by the test suite).

### Market data (`src/data/market_data.py`)

```python
class MarketDataProvider(ABC):
    def get_candles(self, symbol: str, timeframe: str = "1Day",
                    limit: int = 250) -> list[Candle]: ...
    def get_last_price(self, symbol: str) -> float: ...   # default: last daily close
```

`Candle(ts, open, high, low, close, volume)`. Two implementations: Alpaca and CSV.

### Strategy (`strategies/base.py`)

```python
class Strategy(ABC):
    name: str
    timeframe: str
    def evaluate(self, snapshot: TechnicalSnapshot) -> Optional[StrategySetup]: ...
```

Contract: **must never raise.** Strategies are mechanical pre-filters that decide
whether a symbol is worth a model call at all, which is what keeps token spend
proportional to opportunity. Two included: `trend_breakout`, `mean_reversion`.

---

## 6. Config surface

`src/config.py` — pydantic-settings, `.env`-driven, ~45 fields. Two pieces worth
copying regardless of what else you take:

**Key hygiene.** Every string field is stripped on load. A pasted key with a trailing
space producing a mystery 401 is the single most common setup failure.

**The live-trading interlock.** Real orders require three independent switches to
agree:

```python
TRADING_MODE=live
ALPACA_PAPER=false
CONFIRM_LIVE=I_UNDERSTAND_THE_RISK
```

`Settings.live_trading_enabled` is the only thing allowed to authorise a real order,
and `live_mode_blockers()` reports which switches are still holding. `ExecutionEngine`
re-checks it immediately before submitting, as a backstop. Port this pattern — a
single `is_live` boolean is one typo away from a real order.

---

## 7. Tests worth porting

142 pass, zero network calls. The ones that encode real decisions, not coverage:

| File | Tests | What it pins down |
|---|---|---|
| `test_risk.py` | 17 | Every gate rule, including both circuit breakers |
| `test_strategies.py` | 16 | Each rule fires only when *all* conditions hold; degenerate input returns `None` |
| `test_indicators.py` | 13 | Hand-checkable values; **no NaN/inf in the prompt payload** (NaN is not valid JSON and would break every call) |
| `test_journal.py` | 13 | R-multiple arithmetic for long *and* short, fees, stop-exit = −1R |
| `test_metrics.py` | 11 | Low win rate + high avg R is profitable; profit factor is *not* infinite on a thin sample |
| `test_execution.py` | 10 | **Alert mode orders nothing; double-approve cannot double-order; a live broker without confirmation is blocked** |
| `test_sizing.py` | 9 | Wider stop = smaller position, same dollar risk; each cap |
| `test_pipeline.py` | 9 | Full chain end-to-end against a stubbed model; **drawdown halt spends zero tokens** |
| `test_backtest.py` | 8 | Lookahead guard; plus a test asserting the fixture generates trades at all |
| `test_approvals.py` | 8 | Expiry, idempotent resolve |

Two patterns to carry over:

- **`test_backtest.py::test_the_fixture_actually_produces_trades`** — guards the other
  tests in that file from passing vacuously on an empty trade list. The first version
  of that fixture generated zero setups and the lookahead test silently skipped.
- **`test_execution.py`** uses a `RecordingBroker` that appends to a list. Asserting
  `broker.orders == []` is how you prove alert mode is actually inert.

---

## 8. Known gaps — read before relying on any of this

Ordered by how likely each is to bite you.

1. **The AI path has never run against a live model.** Every model call in testing was
   a deliberate 401 or a stub. The prompts are faithful to the guide and the
   setup-parsing is unit-tested against synthetic JSON, but `Analyst.find_setup` has
   not seen real Claude output. **First thing to do with real keys:** run
   `python -m src.cli analyze AAPL` and `scan --always-analyse`, and check that
   structured output parses and that confidence values are calibrated rather than
   uniformly 0.9.

2. **No signal has been generated against real market data.** All verification used
   synthetic CSVs from `scripts/make_sample_data.py`, which is *random data* — any P&L
   it produces is meaningless and the script says so in its own output.

3. **The IG adapter has never run against a real IG account.** It is written to
   IG's published REST API and tested against a mocked IG (81 tests covering the wire
   format, the asynchronous deal/confirm sequence, closing via `_method: DELETE`, and
   epic resolution), but no real credentials have ever been typed into it. Expect
   wire-level surprises on first contact and find them on a demo account. It also does
   not handle IG's `scalingFactor` (some FX markets quote x100) or guaranteed stops.
   See [ig-spread-betting.md](ig-spread-betting.md).

4. **Crypto has no bracket orders.** Alpaca does not support them for crypto, so the
   stop is tracked bot-side only. A process death between fill and stop placement
   leaves an unprotected position. `alpaca.py` logs
   `broker.crypto_no_bracket` when this applies. If your platform supports crypto
   brackets, this gap closes; if not, treat crypto as needing an external stop watcher.

5. **`SimulatedBroker` is not a market simulator.** Fills at the signal price. No
   slippage, no partial fills, no queue position, no spread. Fine for wiring
   verification, useless for estimating returns.

6. **The backtester tests mechanical rules only, not the AI.** You cannot reproduce
   what a model said six months ago. It does guard lookahead (decisions use bars
   `0..i`, fills at `i+1` open) and assumes stop-before-target when a bar contains
   both — the pessimistic reading. Treat its output as an upper bound.

7. **Journal reconciliation is one-directional.** The bot records what it submitted; it
   does not poll the broker to confirm what actually filled, or detect a stop that
   triggered while it was asleep. Open positions are read live from the broker, but
   `trades` rows close only when the bot closes them. **If you integrate with a real
   platform, wire fill/stop webhooks into `TradeJournal.close_trade()`** or the metrics
   will drift from reality. This is the largest missing piece for production use.

8. **Approval expiry is wall-clock, not price-based.** A 45-minute-old approval is
   refused, but a 5-minute-old approval on a symbol that just gapped is accepted. A
   price-drift check before submission would be a sensible addition.

9. **Single-process, single-user.** The approval store is a JSON file and the journal is
   SQLite with `check_same_thread=False`. Two concurrent writers will race. Move both
   to your platform's database before running more than one instance.

10. **`.gitignore` lesson, since it cost us a package.** A bare `data/` rule also matches
   `src/data/` and silently excluded that entire package from the first commit — tests
   passed locally against files git was ignoring, and the next clean checkout could not
   import. Fixed in `e5822e9` by anchoring every rule that names a real source
   directory (`/data/`, `/logs/`, `/build/`, …). **If you copy the `.gitignore`, copy
   the anchored version**, and after any large first commit run
   `find . -name '*.py' | git check-ignore --stdin` to confirm nothing vanished.

---

## 9. Verification checklist for the port

Run these in order; each is cheap and catches a different class of mistake.

```bash
# 1. Nothing is silently ignored (the §8.9 failure)
find . -name '*.py' -not -path './.git/*' | git check-ignore --stdin   # expect: no output

# 2. Everything imports from a clean checkout in a fresh venv
python -m venv venv && ./venv/bin/pip install -r requirements-trader.txt pytest
./venv/bin/python -c "import src.pipeline, src.risk, src.cli"

# 3. The suite passes with no network
./venv/bin/python -m pytest tests/ -q                  # expect: 142 passed

# 4. Config and key hygiene
./venv/bin/python -m src.cli doctor                    # offline checks
./venv/bin/python -m src.cli doctor --check-apis       # one real call per API

# 5. The pipeline runs end to end with no keys at all
./venv/bin/python scripts/make_sample_data.py
./venv/bin/python -m src.cli backtest --symbols AAPL,MSFT --bars 400
./venv/bin/python -m src.cli scan --simulated

# 6. The gate actually blocks — assert on this in your own suite
#    alert mode must leave your broker's order list empty
```

`doctor` is worth porting on its own: it checks Python version, packages, `.env`
presence, keys with surrounding whitespace, whether `.env` is gitignored, which
live-mode blockers are active, and optionally makes one live call per API.

---

## 10. Where to start reading

In this order, and no further than you need:

1. `src/risk.py` — the rules, and the comment explaining why each is a hard reject
2. `src/sizing.py` — 60 lines, the whole sizing argument
3. `src/models.py` — the vocabulary everything else speaks
4. `src/pipeline.py` — how one cycle fits together (read as a spec)
5. `src/execution.py` — the approval gate and the live-order backstop
6. `tests/test_pipeline.py` — the chain working, end to end, in one file

Open questions for whoever picks this up: which side owns the journal (§8.6), and
whether your platform can submit brackets atomically (§5).
