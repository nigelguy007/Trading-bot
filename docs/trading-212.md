# Using Trading 212 as the broker

Trading 212 is the most benign thing this bot can trade: you own actual
shares, there is no leverage, and inside a Stocks ISA the gains are untaxed.

It also has one gap that nothing in this repo can close, and you need to
understand it before you fund anything.

---

## The gap: no bracket orders

Every other adapter here sends entry, stop and target as **one** instruction,
so the stop exists at the broker from the instant the entry fills. Trading
212's API has four separate order endpoints and no way to attach anything to
an entry. The best available sequence is:

```
1. POST /orders/market      buy
2. GET  /orders/{id}        poll until FILLED
3. POST /orders/stop        sell stop   <- the protection
4. POST /orders/limit       sell limit  <- the target
```

That leaves two holes:

**Between (1) and (3) the position has no stop.** Usually a second or two.
If the bot dies in that window you own shares with nothing protecting them.
On the next start `reconcile()` finds them and tells you, but a crash plus an
overnight gap is a real loss this bot cannot prevent.

**(3) and (4) are not OCO-linked.** When the target fills, the stop stays
live — a sell order against shares you no longer own. `reconcile()` cancels
those, but only while the bot is running.

Both are the price of trading real shares in an ISA through this API. If you
want a broker-side stop that exists from the instant of the fill, use IG or
Alpaca.

### What the bot does about it

| | |
|---|---|
| Stop before target | If only one can be placed it must be the one that caps the loss |
| A rejected stop | Reported as **accepted with a warning**, because the position really is open. The message tells you to place a stop in the app now |
| A rejected target | A warning only — the trade just runs to its stop |
| An entry that never fills | No stop is placed and nothing is left dangling |
| `reconcile()` | Runs on startup and every scan: cancels orphaned sell orders, and logs `broker.t212_UNPROTECTED_POSITIONS` for anything held with no resting stop |

Keep `EXECUTION_MODE=alert` (the default) on T212 for longer than you
otherwise would. The unprotected window is short, but you want to have seen
the sequence work a few dozen times before you stop watching it.

---

## Other things the API decides for you

- **Long only.** Invest and ISA accounts cannot short. A short signal is
  refused outright rather than silently dropped — a skipped signal looks like
  the bot working when it isn't.
- **Invest and Stocks ISA only.** The API is not enabled for CFD accounts.
- **Side is the sign of the quantity.** There is no `side` field: `-10` is a
  sell. A sign error here is a buy where you meant a sell.
- **No price data at all.** There is no quote or candle endpoint. See below.
- **Orders are not idempotent** — T212 says so explicitly. Nothing in the
  adapter retries a POST.
- **Tickers are T212's own** (`AAPL_US_EQ`), resolved from
  `/metadata/instruments` and cached hard, because that endpoint allows one
  request per 50 seconds.
- **Rate limits are per endpoint and tight**: `/positions` 1/s,
  `/account/summary` 1/5s, stop and limit orders 1 per 2s. The adapter spaces
  calls client-side, because a 429 landing between an entry and its stop is
  the expensive one.

---

## Prices come from Alpaca

Trading 212 has no price API, so with `BROKER=t212` the bot reads bars from
**Alpaca** instead. Free paper keys are enough — they are used for **data
only**, and no order ever goes near them.

This is sound here in a way it would not be for IG: T212 holds the actual
share and Alpaca quotes that same share at the same scale. An IG spread bet
can be quoted in a different unit entirely, which is why that combination
isn't offered.

```
BROKER=t212
T212_API_KEY=...
ALPACA_API_KEY=your_paper_key       # DATA ONLY
ALPACA_SECRET_KEY=your_paper_secret
```

`doctor` fails loudly if T212 is configured with no price source, because the
symptom otherwise is a bot that scans nothing and looks like it simply found
no setups.

---

## Setting it up

1. In the Trading 212 app: **Settings → API (Beta)** → generate a key.
   You need an **Invest or Stocks ISA** account; the API is not available for
   CFD accounts.
2. Fill in `.env`:

```
BROKER=t212
T212_API_KEY=your_key
T212_API_SECRET=            # only if your key has a secret
T212_DEMO=true              # demo.trading212.com
ALPACA_API_KEY=...          # for prices
ALPACA_SECRET_KEY=...
WATCHLIST=AAPL,MSFT,NVDA
```

3. Optionally pin tickers so nothing has to be looked up:
   `T212_TICKERS=AAPL=AAPL_US_EQ,VOD=VODl_EQ`
4. `./start.sh doctor --check-apis`, then `./start.sh`.

Demo and live are different hosts, and `T212_DEMO` picks which. A key
generated for one will not work against the other.

---

## Going live

Three switches, and T212's is `T212_DEMO`:

```
TRADING_MODE=live
T212_DEMO=false
CONFIRM_LIVE=I_UNDERSTAND_THE_RISK
```

A leftover `ALPACA_PAPER=false` or `IG_DEMO=false` does not authorise live
Trading 212 orders — each broker's gate is its own.

---

## Honest status

Written against Trading 212's published OpenAPI document, which is vendored
at `tests/fixtures/t212-openapi.json`. Every request body the adapter sends
and every response field it reads is checked against that spec by
`tests/test_t212_spec.py`, so a field renamed upstream fails a test instead
of an order. `tests/test_t212_broker.py` covers the wire format, the
entry/stop/target sequence, the long-only refusal and `reconcile()`.

**It has never run against a real Trading 212 account.** No credentials have
been typed into it. Find the inevitable surprises on the demo environment:

1. `./start.sh doctor --check-apis` — proves the key, the account and prices.
2. Leave `EXECUTION_MODE=alert` and approve each trade by hand.
3. Place a few small demo trades and check in the app that the stop and the
   target both appear after the entry fills.
4. Deliberately let one hit its target, then confirm the orphaned stop is
   gone after the next scan. That is `reconcile()` doing the job T212's API
   doesn't.
5. Only then consider leaving it unattended, and only on demo.

The API is itself in beta and under active development, so expect it to move.
