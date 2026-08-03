"""
Generate synthetic OHLCV CSVs so the pipeline can be exercised with no API keys.

    python scripts/make_sample_data.py --symbols AAPL,MSFT --bars 400

Writes data/csv/<SYMBOL>.csv, which CSVMarketData reads when Alpaca keys are
absent.

>>> THIS IS RANDOM DATA. <<<

It exists to prove the wiring works — that a scan runs, the risk gate fires,
the journal fills in, the backtester completes. Any P&L it produces is
meaningless: a backtest on invented prices measures nothing about a strategy.
Point the bot at real market data before you believe a single number.
"""
from __future__ import annotations

import argparse
import csv
import math
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def generate(symbol: str, bars: int, seed: int) -> list[dict]:
    # Seeded per symbol so reruns are reproducible.
    rng = random.Random(f"{symbol}:{seed}")
    price = rng.uniform(40, 300)
    drift = rng.uniform(-0.0004, 0.0009)
    vol = rng.uniform(0.010, 0.025)
    base_volume = rng.uniform(5e5, 5e6)

    start = datetime.now(timezone.utc) - timedelta(days=int(bars * 1.45))
    rows: list[dict] = []
    day = start

    for _ in range(bars):
        day += timedelta(days=1)
        while day.weekday() >= 5:  # skip weekends so the series looks like a tape
            day += timedelta(days=1)

        shock = rng.gauss(drift, vol)
        open_price = price
        close = max(0.5, price * math.exp(shock))
        span = abs(close - open_price) + price * abs(rng.gauss(0, vol * 0.6))
        high = max(open_price, close) + span * rng.random()
        low = min(open_price, close) - span * rng.random()

        rows.append(
            {
                "ts": day.replace(hour=20, minute=0, second=0, microsecond=0).isoformat(),
                "open": round(open_price, 2),
                "high": round(high, 2),
                "low": round(max(0.01, low), 2),
                "close": round(close, 2),
                # Volume rises with the size of the move, as it does in reality.
                "volume": int(base_volume * (1 + abs(shock) / vol * 0.8) * rng.uniform(0.7, 1.3)),
            }
        )
        price = close

    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split(">>>")[0])
    parser.add_argument("--symbols", default="AAPL,MSFT,NVDA,SPY")
    parser.add_argument("--bars", type=int, default=400)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--out", default="data/csv")
    args = parser.parse_args()

    out_dir = REPO_ROOT / args.out
    out_dir.mkdir(parents=True, exist_ok=True)

    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    for symbol in symbols:
        rows = generate(symbol, args.bars, args.seed)
        path = out_dir / f"{symbol.replace('/', '-')}.csv"
        with path.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=["ts", "open", "high", "low", "close", "volume"])
            writer.writeheader()
            writer.writerows(rows)
        print(f"  wrote {path.relative_to(REPO_ROOT)}  ({len(rows)} bars)")

    print(
        f"\n{len(symbols)} synthetic series written to {args.out}.\n"
        "This is RANDOM DATA for smoke-testing the pipeline. Results from it\n"
        "say nothing about any strategy — use real market data before drawing\n"
        "any conclusion.\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
