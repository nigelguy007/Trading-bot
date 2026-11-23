"""
Pull years of 1-minute bars from Databento and turn them into backtest data.

    export DATABENTO_API_KEY=db-...
    python scripts/fetch_databento.py --symbol ES.v.0 --name ES            # cost check only
    python scripts/fetch_databento.py --symbol ES.v.0 --name ES --yes      # download + resample

Defaults pull the E-mini S&P 500 continuous future from CME (GLBX.MDP3) from
2010-06-07, when Databento's CME history begins, to the latest available day.

Writes:
  data/databento/<NAME>_ohlcv-1m.csv.gz   the raw 1-minute bars (kept for later)
  data/csv/<NAME>.csv                     daily bars the backtester reads

Spending is capped: the free cost check runs first, and the download refuses
to start if the quote is over --max-cost (default $20) or if --yes is missing.

Note on instruments: CME futures cannot be traded through Alpaca, IG or
Trading 212 as-is. ES is a fine research proxy for SPY (Alpaca/T212) or the
US 500 spread bet (IG), but validate on the instrument you will actually trade
before risking money — fees, hours and gaps differ.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from src.data.databento import (  # noqa: E402
    DatabentoClient,
    DatabentoError,
    RangeRequest,
    resample_daily,
    session_for,
)

DEFAULT_MAX_COST = 20.0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--symbol", default="ES.v.0", help="Databento symbol (default ES.v.0)")
    p.add_argument("--name", help="name for the bot's CSV (default: root of --symbol, e.g. ES)")
    p.add_argument("--dataset", default="GLBX.MDP3")
    p.add_argument("--start", default="2010-06-07")
    p.add_argument("--end", help="exclusive end date; omit for latest available")
    p.add_argument("--max-cost", type=float, default=DEFAULT_MAX_COST,
                   help=f"refuse to download above this many USD (default {DEFAULT_MAX_COST:g})")
    p.add_argument("--yes", action="store_true", help="actually download (spends money)")
    p.add_argument("--resample-only", action="store_true",
                   help="skip the API; rebuild data/csv from an existing raw file")
    p.add_argument("--no-adjust", action="store_true", help="do not back-adjust futures rolls")
    args = p.parse_args(argv)

    name = (args.name or args.symbol.split(".")[0]).upper()
    raw = REPO_ROOT / "data" / "databento" / f"{name}_ohlcv-1m.csv.gz"
    daily = REPO_ROOT / "data" / "csv" / f"{name}.csv"
    req = RangeRequest(args.dataset, args.symbol, args.start, args.end)

    try:
        if not args.resample_only:
            client = DatabentoClient(os.environ.get("DATABENTO_API_KEY", ""))
            try:
                cost = client.get_cost(req)
                print(f"Quote: {args.symbol} {req.schema} {args.start} → "
                      f"{args.end or 'latest'} on {args.dataset} = ${cost:,.2f}")
                if cost > args.max_cost:
                    print(f"Over the ${args.max_cost:,.2f} cap. Nothing downloaded. "
                          "Narrow the range or raise --max-cost deliberately.")
                    return 2
                if not args.yes:
                    print("Cost check only. Re-run with --yes to download.")
                    return 0
                rows = client.download(req, raw)
                print(f"Downloaded {rows:,} one-minute bars → {raw.relative_to(REPO_ROOT)}")
            finally:
                client.close()
        elif not raw.exists():
            print(f"No raw file at {raw}", file=sys.stderr)
            return 1

        summary = resample_daily(raw, daily, session_for(args.dataset), back_adjust=not args.no_adjust)
    except DatabentoError as exc:
        print(f"Databento: {exc}", file=sys.stderr)
        return 1

    print(
        f"Resampled {summary.minute_bars:,} minute bars → {summary.daily_bars:,} daily bars "
        f"({summary.first_day} → {summary.last_day}), {summary.rolls} rolls "
        f"{'back-adjusted' if not args.no_adjust else 'NOT adjusted'} → {daily.relative_to(REPO_ROOT)}"
    )
    print(f"Next: python -m src.cli backtest --symbols {name} --start 2010-06-07 "
          f"--split 2020-01-01 --slippage-bps 1 --commission-per-unit 2.25")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
