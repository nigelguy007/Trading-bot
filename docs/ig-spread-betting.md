# Using IG (IG Index / IG Markets) as the broker

Read this before you put money anywhere near it. IG is not a drop-in
replacement for Alpaca, and the difference is not cosmetic.

---

## The one thing you must understand

**Alpaca buys shares. IG does not.**

| | Alpaca | IG |
|---|---|---|
| What you get | You own the shares | A bet on the price. You own nothing |
| Size is measured in | Shares (50 shares of AAPL) | Currency **per point** (£2.50/point) |
| Leverage | None by default | Built in, always |
| £500 buys you | £500 of stock | often £5,000–£10,000 of exposure |
| Can you lose more than you put in? | No | Possible on gaps. UK retail clients have negative balance protection, but a gapped stop still loses far more than planned |
| Demo account | Unleveraged paper shares | **Spread bet / CFD only — leveraged** |
| UK tax | CGT + stamp duty apply | Spread bets currently free of both; CFDs are not. Depends on your circumstances and changes |

That last demo row matters: there is no unleveraged IG demo. If you open an IG
demo to practise, you are practising a geared product.

### What leverage does to the bot's risk rules

The risk gate still sizes every trade so that **if the stop fills at its
level**, you lose at most `RISK_PER_TRADE_PCT` of equity. That arithmetic is
unchanged by gearing — the stake is simply smaller.

What gearing changes is the tail. When price **gaps through** your stop
overnight or on news, you are filled at the gap, not at your level. On shares a
gap costs you a bit more than planned. On a 5x geared position it costs you
several times more than planned. The bot cannot fix that. Your options are:

- Keep `IG_MAX_EXPOSURE_PCT` low (the default 300% is already a choice, not a
  law — 100% means "no more exposure than equity").
- Trade markets that gap less (major indices and FX rather than single small
  caps).
- Use IG's **guaranteed stops**, which fill at your level whatever happens. IG
  charges a premium for them. **This adapter sends `guaranteedStop: false`** —
  if you want them, that is a code change in `src/broker/ig.py`.

---

## Setting it up

### 1. Get an IG account and an API key

1. Open an IG demo account at <https://www.ig.com> (free, no funding).
2. Log in, go to **My IG → Settings → API keys**.
3. Generate a key. Note which environment it is for.
4. **Turn OFF "encrypted passwords"** for that key. This adapter sends a plain
   login over HTTPS; the encrypted variant is not implemented and IG will
   refuse the session with
   `error.public-api.failure.encryption.required`.

> **Demo and live keys are not interchangeable.** A demo key only works against
> `demo-api.ig.com`. If `IG_DEMO` does not match the key you generated, IG
> returns `error.security.api-key-invalid` and nothing works. This is the single
> most common setup failure.

### 2. Fill in `.env`

```
BROKER=ig
IG_API_KEY=your_key_from_my_ig
IG_USERNAME=your_ig_web_login
IG_PASSWORD=your_ig_web_password
IG_DEMO=true
IG_CURRENCY=GBP
```

`IG_USERNAME` / `IG_PASSWORD` are your **web login**, not the API key. The
username is case-sensitive.

### 3. Pin your epics

IG does not use tickers. It uses **epics** — its own instrument codes:

| What you mean | IG epic |
|---|---|
| FTSE 100 cash | `IX.D.FTSE.DAILY.IP` |
| Wall Street (Dow) cash | `IX.D.DOW.DAILY.IP` |
| Apple | `UA.D.AAPL.CASH.IP` |
| EUR/USD | `CS.D.EURUSD.MINI.IP` |
| Spot Gold | `CS.D.USCGC.TODAY.IP` |

Find the epic by opening the market on IG's own platform and reading it off the
URL. Then:

```
WATCHLIST=FTSE,AAPL,MSFT
IG_EPICS=FTSE=IX.D.FTSE.DAILY.IP,AAPL=UA.D.AAPL.CASH.IP,MSFT=UA.D.MSFT.CASH.IP
```

Anything you do not pin is resolved by IG's own market search, which ranks by
IG's relevance, not yours. The bot prefers a non-expiring ("cash"/"daily")
market over a dated future and logs a loud `broker.ig_epic_guessed` warning
with the instrument name so you can check it. **Check it.** A guessed epic can
be a dated future at a different price, or an entirely different market.

### 4. Verify

```bash
./start.sh doctor --check-apis
```

You should see your IG equity, your available margin, and for each of the first
three symbols: the resolved epic, IG's minimum stake, IG's minimum stop
distance, and whether the market is currently tradeable.

If `doctor` is happy, start the dashboard:

```bash
./start.sh
```

---

## How sizing works on IG

Same formula, different unit:

```
stake per point = (equity x RISK_PER_TRADE_PCT / 100) / stop distance in points
```

Worked example — £10,000 account, 1% risk, FTSE at 8000 with a stop at 7960:

```
risk budget     = £10,000 x 1%        = £100
stop distance   = 8000 - 7960         = 40 points
stake           = £100 / 40           = £2.50 per point
exposure        = £2.50 x 8000        = £20,000   (200% of equity)
if stopped out  = £2.50 x 40          = £100      (the 1% you budgeted)
```

Note the exposure line. £20,000 of exposure on a £10,000 account is normal for
a spread bet and is why `MAX_POSITION_PCT` is **not** applied here — a 50%
position cap would have cut this to £0.625/point and quietly reduced your risk
to 0.25%. `IG_MAX_EXPOSURE_PCT` governs instead.

Three things can shrink the stake, and the alert always says which:

1. **Gearing cap** — exposure above `IG_MAX_EXPOSURE_PCT` of equity.
2. **Available margin** — a rough `IG_MARGIN_PCT` check, to avoid sending IG a
   stake it will obviously refuse. IG is the real authority.
3. **IG's minimum stake** — below `IG_MIN_DEAL_SIZE` the trade is **refused
   entirely** rather than rounded up, because rounding up would risk several
   times your budget. A small account with wide stops will hit this.

Stakes round **down** to 2 decimal places, so rounding can only reduce risk.

---

## How orders work on IG

An entry, its stop and its target go out as **one** request
(`POST /positions/otc` with `stopLevel` and `limitLevel` attached). This is
better than Alpaca's bracket: there is no window at all in which the position
exists without a stop.

`forceOpen: true` is required for that and is always sent. Without it IG may
net the deal against an existing position in the same market and silently drop
the attached levels.

IG's dealing is **asynchronous**: the POST returns a `dealReference`, and you
then fetch `GET /confirms/{dealReference}` to find out what happened. Three
outcomes:

| Confirm says | Bot reports |
|---|---|
| `ACCEPTED` | filled, with the actual fill level |
| `REJECTED` | not accepted, with IG's reason translated into English |
| couldn't be read | **accepted, status `unconfirmed`**, with a message telling you to go and look |

That third row is deliberate and important. If IG took the deal and the confirm
call then failed, reporting "rejected" would invite you — or a retry — to send
it again, and now you have two geared positions. The bot never auto-retries a
write for this reason: reads re-authenticate and retry, writes never do.

Closing uses `POST /positions/otc` with an `_method: DELETE` header and the
opposite direction. A real HTTP DELETE returns 405.

---

## IG's rate and data limits

These are tighter than Alpaca's and will bite you.

- **Historical prices are metered weekly**: 10,000 data points per week on a
  live account. A 250-bar daily history for 8 symbols is 2,000 points, so four
  full refreshes and your week is gone. Daily bars are therefore cached for six
  hours and the remaining allowance is logged; when it drops below 20% you get
  a `marketdata.ig_allowance_low` warning. **Keep `WATCHLIST` to 3–5 symbols
  on IG.**
- **Request rate** is per-minute and a scan bursts. `IG_MIN_REQUEST_INTERVAL`
  (default 1 second) spaces calls out. Raise it if you see 429s.
- **Session tokens expire.** The adapter re-authenticates automatically on a
  read.

---

## What IG cannot do here

- **No crypto.** IG offers no crypto to UK retail clients. `CRYPTO_WATCHLIST`
  entries are refused by the broker router rather than sent somewhere they
  cannot fill, and `doctor` warns if you have set any.
- **No market-wide clock.** Every IG market has its own hours. `is_market_open`
  checks the first configured epic as a stand-in and is deliberately permissive
  when it cannot tell — a false "closed" would silently stop the bot for a day,
  whereas a false "open" costs one API call, because IG refuses deals on closed
  markets anyway.
- **No guaranteed stops** (see above).
- **No `scalingFactor` handling.** Some IG markets quote prices scaled by 100
  (certain FX pairs). `dealing_rules()` reports the factor but nothing applies
  it. If your levels look off by 100x on a market, this is why.

---

## Going live

Live trading needs **three** switches, and IG's is `IG_DEMO`:

```
TRADING_MODE=live
IG_DEMO=false
CONFIRM_LIVE=I_UNDERSTAND_THE_RISK
```

All three, plus a **live** API key (the demo one will not work against the live
host). A leftover `ALPACA_PAPER=false` does not authorise live spread bets —
each broker's gate is its own.

`./start.sh doctor` names whichever switch is still holding it back.

---

## Honest status of this adapter

Written against IG's published REST API and tested against a mocked IG
(`tests/test_ig_broker.py`, `tests/test_ig_sizing.py`,
`tests/test_ig_market_data.py`, `tests/test_ig_execution.py` — 81 tests
covering the wire format, the
deal/confirm sequence, the unconfirmed-deal case, closing, epic resolution,
rate limiting, price parsing and the stake arithmetic).

**It has never run against a real IG account.** Nobody has typed real
credentials into it. Expect to find small wire-level surprises on first
contact, and find them on a demo account:

1. `./start.sh doctor --check-apis` — proves login, account, epics.
2. Leave `EXECUTION_MODE=alert` (the default) so you approve every trade by
   hand and can compare what the bot drafted against what IG actually did.
3. Place a handful of **minimum-stake** demo deals and check the fill level,
   the stop and the target on IG's own platform.
4. Only then consider `EXECUTION_MODE=auto`, and only on demo.
5. Live last, small, and with `IG_MAX_EXPOSURE_PCT` turned down.
