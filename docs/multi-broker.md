# Running two brokers as one portfolio

You can run IG and Trading 212 (or any pair) side by side: spread bet the
index at IG, buy the share at Trading 212, one set of risk rules over both.

This page is mostly about why that needs real machinery rather than two
copies of the bot.

---

## Why not just run two bots

Because every circuit breaker is a percentage of equity, and two bots have
two equities.

| Rule | Two separate bots | One combined portfolio |
|---|---|---|
| `DAILY_LOSS_LIMIT_PCT=3` | −3% at each venue = **−6% day, nothing fired** | −3% of the total stops trading |
| `MAX_DRAWDOWN_PCT=15` | Two separate high-water marks | One mark on the combined equity |
| `MAX_OPEN_POSITIONS=5` | 5 each, so **10** | 5 in total |
| `MAX_CORRELATED_POSITIONS=2` | Cannot see that a FTSE bet at IG and a UK tracker at T212 are the same bet | Counts both |
| `MAX_PORTFOLIO_RISK_PCT=6` | 6% each, so **12%** | 6% of the whole book |

The daily-stop row is the one that matters. It is a limit that looks armed
and is not, and you would only discover it on the day it should have fired.

So `PortfolioManager` reads **every** configured venue, sums equity, cash and
buying power, concatenates the positions tagged with the venue holding them,
and hands the risk gate one picture. The rules are unchanged — they just
finally see the whole book.

---

## Setting it up

```
BROKER=ig                   # primary
SECONDARY_BROKER=t212       # runs alongside, same portfolio
BROKER_ROUTES=FTSE=ig,UK100=ig,AAPL=t212,MSFT=t212
```

Plus each broker's own keys. Routing is resolved most specific first:

1. **`BROKER_ROUTES`** names a venue for a symbol.
2. **Asset class**, for the split the venues decide themselves — only Alpaca
   handles crypto, so crypto goes there whatever else is running.
3. **The primary broker** for everything else.

Level 1 exists because IG and T212 are both "equity" as far as a signal is
concerned, so asset class cannot tell them apart — and choosing between them
is the whole reason to run two.

`doctor` checks the parts that go wrong quietly:

- a route naming a broker that isn't configured (**fails** — those symbols
  would silently go to the primary)
- a second broker configured with nothing routed to it (**warns** — it is
  being read for risk but never traded)

---

## Sizing is per venue

The same equity and the same stop mean different things at different brokers:

```
£10,000 equity, 1% risk, entry 100, stop 98

  at Trading 212   50 shares          (unleveraged, £5,000 committed)
  at IG            £50 a point        (geared, £5,000 of exposure on margin)
```

So `calculate_size` takes the target venue and returns the right unit, and
`PositionSizing.unit` carries it through the alert, the dashboard and the
approval queue. A stake per point displayed as a bare number reads as a
position several times smaller than it is.

---

## When a venue goes down

Trading halts. Not because the bot is fragile, but because the alternative
is worse: equity is short by the whole of the missing account, and every
limit is a percentage of equity, so anything opened now would be sized
against a number known to be wrong.

While a venue is unreachable:

- **No new trades.** `check_circuit_breakers` refuses with the venue named.
- **Existing positions are untouched**, and so are their broker-side stops.
  Nothing is force-closed.
- **No drawdown is reported.** Measuring half a book against the full-book
  peak invents one — with every venue down it reads 100%.
- **Nothing is written to the equity curve.** A short reading would become
  tomorrow's start-of-day baseline and a false drawdown long after the
  outage was forgotten.
- The dashboard shows a banner naming the venue.

It resumes by itself on the next cycle that reads cleanly.

---

## Day P&L without a broker baseline

The daily loss limit needs to know where equity started today. Alpaca reports
`last_equity`; **Trading 212 reports nothing of the kind** and IG's is an
approximation.

So when a broker gives no baseline, the figure comes from the journal's first
equity reading of the day. Without that fallback the daily stop would read
0.00% every day and never fire — the worst way for a circuit breaker to fail,
because it looks like it is working.

A partial sum is never used: if one venue reports a baseline and the other
does not, the sum is discarded and the journal is used instead. Half a
baseline produces a confident, wrong percentage.

---

## What is still single-venue

Honest list of what this work did **not** do:

- **Market data.** Prices come from one source, chosen by the primary broker
  (IG from IG, T212 and Alpaca from Alpaca). Fine while both venues trade the
  same underlying; be careful if they don't.
- **`is_market_open`** asks the primary broker only.
- **Journal stops** are keyed by symbol, with a venue-qualified key preferred.
  The same ticker held at both venues with different stops is read correctly,
  but older rows written before this change are symbol-only.
- **Correlation groups** are by symbol, so the same company under two
  tickers at two venues (`VOD` and `VODl_EQ`) is not recognised as one
  position. Pin both names to one group in `config/correlation_groups.json`
  if that matters to you.
