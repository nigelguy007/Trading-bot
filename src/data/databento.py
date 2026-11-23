"""
Databento historical data — years of 1-minute bars for backtesting.

Three stages, kept separate so each can be re-run on its own:

  1. cost      `metadata.get_cost` — FREE. Always asked first, and the download
               refuses to start above a cap. Databento bills per byte, so a
               typo'd symbol list can cost real money.
  2. download  `timeseries.get_range` streamed straight to a gzip file. Sixteen
               years of one futures contract is ~6M rows; it never sits in
               memory.
  3. resample  1-minute → daily bars in the bot's CSV format, with the trading
               day defined by the exchange session (CME's day starts 17:00
               Chicago, not midnight UTC), and futures rolls back-adjusted.

Why back-adjust: a continuous future (ES.v.0) is the front contract, then the
next one, glued together. At each roll the price jumps by the gap between the
two contracts — a jump no trader experienced. Left in, it fires stops and
breakouts that never happened. The Panama adjustment shifts every bar BEFORE a
roll by that gap, so the series is continuous while the most recent prices stay
real. Price LEVELS deep in the past are then shifted (by tens of points on ES),
which is fine for signals and R-multiples but means old %-returns are slightly
off. The roll gap is estimated as new-contract first open minus old-contract
last close, so it also absorbs the roll session's own overnight move — small on
CME, where the break is one hour.
"""
from __future__ import annotations

import csv
import gzip
import re
from bisect import bisect_right
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator
from zoneinfo import ZoneInfo

import httpx

from src.logging_setup import get_logger

log = get_logger(__name__)

HIST_URL = "https://hist.databento.com/v0"
SCHEMA_1M = "ohlcv-1m"
_CONTINUOUS = re.compile(r"^.+\.[cnv]\.\d+$")  # ES.c.0, CL.v.1, NQ.n.0


class DatabentoError(RuntimeError):
    pass


@dataclass(frozen=True)
class Session:
    """When a trading day starts, in the exchange's own clock."""

    tz: str
    start_hour: int  # 17 = the day starts at 17:00 the evening before

    def trade_date(self, ts: datetime) -> date:
        local = ts.astimezone(ZoneInfo(self.tz))
        shift = (24 - self.start_hour) % 24
        return (local + timedelta(hours=shift)).date()


CME_SESSION = Session("America/Chicago", 17)
US_EQUITY_SESSION = Session("America/New_York", 0)


def session_for(dataset: str) -> Session:
    return CME_SESSION if dataset.upper().startswith("GLBX") else US_EQUITY_SESSION


def stype_for(symbol: str) -> str:
    return "continuous" if _CONTINUOUS.match(symbol) else "raw_symbol"


@dataclass(frozen=True)
class RangeRequest:
    dataset: str
    symbol: str
    start: str  # ISO date, inclusive
    end: str | None = None  # ISO date, exclusive; None = latest available
    schema: str = SCHEMA_1M

    def params(self) -> dict[str, str]:
        p = {
            "dataset": self.dataset,
            "symbols": self.symbol,
            "schema": self.schema,
            "start": self.start,
            "stype_in": stype_for(self.symbol),
        }
        if self.end:
            p["end"] = self.end
        return p


class DatabentoClient:
    def __init__(
        self,
        api_key: str,
        http: httpx.Client | None = None,
        base_url: str = HIST_URL,
    ) -> None:
        if not api_key:
            raise DatabentoError(
                "DATABENTO_API_KEY is not set. Get one at databento.com "
                "(new accounts include free credit)."
            )
        self.base_url = base_url.rstrip("/")
        self.http = http or httpx.Client(timeout=httpx.Timeout(60.0, read=600.0))
        self.auth = httpx.BasicAuth(api_key, "")

    def close(self) -> None:
        self.http.close()

    def get_cost(self, req: RangeRequest) -> float:
        """USD cost of the request. Free to call."""
        resp = self.http.get(
            f"{self.base_url}/metadata.get_cost",
            params={**req.params(), "mode": "historical-streaming"},
            auth=self.auth,
        )
        _raise_for(resp)
        try:
            return float(resp.json())
        except (ValueError, TypeError) as exc:
            raise DatabentoError(f"unexpected cost response: {resp.text[:200]}") from exc

    def download(self, req: RangeRequest, dest: Path) -> int:
        """
        Stream the range as CSV into a gzip file. Returns the row count.

        Writes to a .part file and renames on success, so an interrupted
        download never masquerades as a complete one.
        """
        dest.parent.mkdir(parents=True, exist_ok=True)
        part = dest.with_name(dest.name + ".part")
        data = {
            **req.params(),
            "encoding": "csv",
            "pretty_px": "true",
            "pretty_ts": "true",
            "map_symbols": "true",
        }
        rows = 0
        with self.http.stream(
            "POST", f"{self.base_url}/timeseries.get_range", data=data, auth=self.auth
        ) as resp:
            if resp.status_code >= 400:
                resp.read()
                _raise_for(resp)
            with gzip.open(part, "wt", encoding="utf-8", newline="") as out:
                for line in resp.iter_lines():
                    if line:
                        out.write(line + "\n")
                        rows += 1
        part.replace(dest)
        rows = max(rows - 1, 0)  # minus the header
        log.info("databento.downloaded", symbol=req.symbol, rows=rows, path=str(dest))
        return rows


def _raise_for(resp: httpx.Response) -> None:
    if resp.status_code < 400:
        return
    detail = resp.text[:300]
    if resp.status_code in (401, 403):
        raise DatabentoError(f"Databento rejected the API key ({resp.status_code}): {detail}")
    raise DatabentoError(f"Databento error {resp.status_code}: {detail}")


# ---------------------------------------------------------------------- #
#  Resampling
# ---------------------------------------------------------------------- #
@dataclass(frozen=True)
class MinuteBar:
    ts: datetime
    instrument_id: str
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass
class ResampleSummary:
    minute_bars: int
    daily_bars: int
    rolls: int
    first_day: date | None
    last_day: date | None


def read_minute_bars(path: Path) -> Iterator[MinuteBar]:
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            yield MinuteBar(
                ts=_parse_ts(row["ts_event"]),
                instrument_id=row.get("instrument_id", ""),
                open=float(row["open"]),
                high=float(row["high"]),
                low=float(row["low"]),
                close=float(row["close"]),
                volume=float(row.get("volume") or 0),
            )


def _parse_ts(raw: str) -> datetime:
    """Databento's pretty_ts is nanosecond ISO-8601; Python stops at micro."""
    if raw.isdigit():  # pretty_ts off: integer nanoseconds since epoch
        return datetime.fromtimestamp(int(raw) / 1e9, tz=timezone.utc)
    return datetime.fromisoformat(raw[:19]).replace(tzinfo=timezone.utc)


def find_rolls(bars: Iterator[MinuteBar]) -> list[tuple[datetime, float]]:
    """(time of first bar on the new contract, price gap new-open minus old-close)."""
    rolls: list[tuple[datetime, float]] = []
    prev: MinuteBar | None = None
    for bar in bars:
        if prev is not None and bar.instrument_id != prev.instrument_id:
            rolls.append((bar.ts, bar.open - prev.close))
        prev = bar
    return rolls


def resample_daily(
    raw_path: Path,
    out_path: Path,
    session: Session,
    back_adjust: bool = True,
) -> ResampleSummary:
    """
    1-minute bars → daily bars in the bot's CSV format (ts,open,high,low,close,volume).

    Two streaming passes over the file: the first finds the rolls, the second
    applies the cumulative adjustment and aggregates by trading day.
    """
    rolls = find_rolls(read_minute_bars(raw_path)) if back_adjust else []
    roll_times = [t for t, _ in rolls]
    # adjustment for a bar = sum of gaps of every roll AFTER it
    suffix = [0.0] * (len(rolls) + 1)
    for k in range(len(rolls) - 1, -1, -1):
        suffix[k] = suffix[k + 1] + rolls[k][1]

    days: dict[date, list[float]] = {}
    minutes = 0
    for bar in read_minute_bars(raw_path):
        minutes += 1
        adj = suffix[bisect_right(roll_times, bar.ts)] if rolls else 0.0
        d = session.trade_date(bar.ts)
        o, h, l, c = bar.open + adj, bar.high + adj, bar.low + adj, bar.close + adj
        agg = days.get(d)
        if agg is None:
            days[d] = [o, h, l, c, bar.volume]
        else:
            agg[1] = max(agg[1], h)
            agg[2] = min(agg[2], l)
            agg[3] = c
            agg[4] += bar.volume

    out_path.parent.mkdir(parents=True, exist_ok=True)
    ordered = sorted(days)
    with out_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["ts", "open", "high", "low", "close", "volume"])
        for d in ordered:
            o, h, l, c, v = days[d]
            ts = datetime(d.year, d.month, d.day, tzinfo=timezone.utc).isoformat()
            writer.writerow([ts, f"{o:.6f}", f"{h:.6f}", f"{l:.6f}", f"{c:.6f}", f"{v:.0f}"])

    return ResampleSummary(
        minute_bars=minutes,
        daily_bars=len(ordered),
        rolls=len(rolls),
        first_day=ordered[0] if ordered else None,
        last_day=ordered[-1] if ordered else None,
    )
