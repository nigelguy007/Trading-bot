"""
Databento loader — the reel's step 1, tested without a key or a cent spent.

What matters: the free cost check comes first and caps spending, the CME
trading day is the session (17:00 Chicago), not the UTC calendar day, and
futures rolls are back-adjusted so they do not fire fake stops.
"""
from __future__ import annotations

import base64
import gzip
from datetime import date, datetime, timezone
from pathlib import Path

import httpx
import pytest

from src.data.databento import (
    CME_SESSION,
    US_EQUITY_SESSION,
    DatabentoClient,
    DatabentoError,
    RangeRequest,
    _parse_ts,
    find_rolls,
    read_minute_bars,
    resample_daily,
    stype_for,
)
from src.data.market_data import CSVMarketData

HEADER = "ts_event,rtype,publisher_id,instrument_id,open,high,low,close,volume,symbol"


def row(ts: str, inst: int, o: float, h: float, l: float, c: float, v: int = 10) -> str:
    return f"{ts},33,1,{inst},{o},{h},{l},{c},{v},ES.v.0"


def write_raw(path: Path, rows: list[str]) -> Path:
    with gzip.open(path, "wt", encoding="utf-8") as fh:
        fh.write("\n".join([HEADER, *rows]) + "\n")
    return path


# --- small pieces ------------------------------------------------------- #
def test_continuous_symbols_are_detected():
    assert stype_for("ES.v.0") == "continuous"
    assert stype_for("CL.c.1") == "continuous"
    assert stype_for("AAPL") == "raw_symbol"
    assert stype_for("ESZ4") == "raw_symbol"


def test_nanosecond_timestamps_parse():
    assert _parse_ts("2020-03-02T14:30:00.123456789Z") == datetime(2020, 3, 2, 14, 30, tzinfo=timezone.utc)
    assert _parse_ts("1583159400000000000") == datetime(2020, 3, 2, 14, 30, tzinfo=timezone.utc)


def test_cme_day_starts_at_five_pm_chicago():
    # Sunday 23:00 UTC = Sunday 17:00 CST → Monday's session.
    assert CME_SESSION.trade_date(datetime(2020, 3, 1, 23, 0, tzinfo=timezone.utc)) == date(2020, 3, 2)
    # Monday 21:00 UTC = 15:00 CST → still Monday.
    assert CME_SESSION.trade_date(datetime(2020, 3, 2, 21, 0, tzinfo=timezone.utc)) == date(2020, 3, 2)
    # Monday 23:00 UTC = 17:00 CST → Tuesday's session.
    assert CME_SESSION.trade_date(datetime(2020, 3, 2, 23, 0, tzinfo=timezone.utc)) == date(2020, 3, 3)


def test_equity_day_is_the_new_york_calendar_day():
    # 02:00 UTC Tuesday = 21:00 Monday in New York.
    assert US_EQUITY_SESSION.trade_date(datetime(2020, 3, 3, 2, 0, tzinfo=timezone.utc)) == date(2020, 3, 2)


# --- resampling --------------------------------------------------------- #
def test_minutes_become_one_daily_bar_per_session(tmp_path):
    raw = write_raw(tmp_path / "es.csv.gz", [
        row("2020-03-01T23:00:00.000000000Z", 1, 100, 101, 99, 100.5),   # Mon session opens Sun evening
        row("2020-03-02T15:00:00.000000000Z", 1, 100.5, 104, 100, 103),
        row("2020-03-02T21:59:00.000000000Z", 1, 103, 103.5, 102, 102.5),
        row("2020-03-02T23:00:00.000000000Z", 1, 102.5, 103, 101, 101.5),  # Tue session
    ])
    out = tmp_path / "ES.csv"
    summary = resample_daily(raw, out, CME_SESSION)

    assert summary.daily_bars == 2
    candles = CSVMarketData(tmp_path).get_candles("ES", limit=10)
    mon = candles[0]
    assert mon.ts.date() == date(2020, 3, 2)
    assert (mon.open, mon.high, mon.low, mon.close, mon.volume) == (100, 104, 99, 102.5, 30)
    assert candles[1].ts.date() == date(2020, 3, 3)


def test_rolls_are_found_and_back_adjusted(tmp_path):
    # Old contract closes at 100; the new one opens at 110 — a 10-point roll gap.
    raw = write_raw(tmp_path / "es.csv.gz", [
        row("2020-03-02T15:00:00.000000000Z", 1, 99, 101, 98, 100),
        row("2020-03-03T15:00:00.000000000Z", 2, 110, 112, 109, 111),
    ])
    assert find_rolls(read_minute_bars(raw)) == [
        (datetime(2020, 3, 3, 15, 0, tzinfo=timezone.utc), 10.0)
    ]

    resample_daily(raw, tmp_path / "ES.csv", CME_SESSION)
    old, new = CSVMarketData(tmp_path).get_candles("ES", limit=10)
    # History before the roll is shifted UP by the gap; recent prices stay real.
    assert (old.open, old.close) == (109, 110)
    assert (new.open, new.close) == (110, 111)


def test_back_adjust_can_be_disabled(tmp_path):
    raw = write_raw(tmp_path / "es.csv.gz", [
        row("2020-03-02T15:00:00.000000000Z", 1, 99, 101, 98, 100),
        row("2020-03-03T15:00:00.000000000Z", 2, 110, 112, 109, 111),
    ])
    summary = resample_daily(raw, tmp_path / "ES.csv", CME_SESSION, back_adjust=False)
    old, _ = CSVMarketData(tmp_path).get_candles("ES", limit=10)
    assert summary.rolls == 0
    assert old.close == 100


# --- the HTTP client ---------------------------------------------------- #
def client_with(handler) -> DatabentoClient:
    return DatabentoClient("db-test-key", http=httpx.Client(transport=httpx.MockTransport(handler)))


def test_cost_check_sends_basic_auth_and_parses_the_price():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers["authorization"]
        seen["params"] = dict(request.url.params)
        return httpx.Response(200, json=12.34)

    cost = client_with(handler).get_cost(RangeRequest("GLBX.MDP3", "ES.v.0", "2010-06-07"))

    assert cost == pytest.approx(12.34)
    assert seen["auth"] == "Basic " + base64.b64encode(b"db-test-key:").decode()
    assert seen["params"]["stype_in"] == "continuous"
    assert seen["params"]["schema"] == "ohlcv-1m"
    assert "end" not in seen["params"]


def test_a_bad_key_says_so():
    c = client_with(lambda r: httpx.Response(401, text="invalid key"))
    with pytest.raises(DatabentoError, match="rejected the API key"):
        c.get_cost(RangeRequest("GLBX.MDP3", "ES.v.0", "2010-06-07"))


def test_no_key_fails_before_any_request():
    with pytest.raises(DatabentoError, match="DATABENTO_API_KEY"):
        DatabentoClient("")


def test_download_streams_to_gzip(tmp_path):
    body = "\n".join([HEADER, row("2020-03-02T15:00:00.000000000Z", 1, 1, 2, 0.5, 1.5)]) + "\n"
    c = client_with(lambda r: httpx.Response(200, text=body))
    dest = tmp_path / "raw.csv.gz"

    assert c.download(RangeRequest("GLBX.MDP3", "ES.v.0", "2020-03-01"), dest) == 1
    assert len(list(read_minute_bars(dest))) == 1
    assert not dest.with_name(dest.name + ".part").exists()


def test_a_failed_download_leaves_no_file(tmp_path):
    c = client_with(lambda r: httpx.Response(422, text="bad symbol"))
    dest = tmp_path / "raw.csv.gz"
    with pytest.raises(DatabentoError):
        c.download(RangeRequest("GLBX.MDP3", "NOPE", "2020-03-01"), dest)
    assert not dest.exists()


# --- the script's spending guard ---------------------------------------- #
@pytest.fixture
def script(monkeypatch, tmp_path):
    import scripts.fetch_databento as fd

    monkeypatch.setattr(fd, "REPO_ROOT", tmp_path)
    monkeypatch.setenv("DATABENTO_API_KEY", "db-test-key")
    calls = {"download": 0}

    class FakeClient:
        def __init__(self, key): pass
        def get_cost(self, req): return calls.get("cost", 5.0)
        def download(self, req, dest):
            calls["download"] += 1
            dest.parent.mkdir(parents=True, exist_ok=True)  # as the real client does
            write_raw(dest, [row("2020-03-02T15:00:00.000000000Z", 1, 1, 2, 0.5, 1.5)])
            return 1
        def close(self): pass

    monkeypatch.setattr(fd, "DatabentoClient", FakeClient)
    return fd, calls


def test_without_yes_only_the_free_quote_runs(script):
    fd, calls = script
    assert fd.main(["--name", "ES"]) == 0
    assert calls["download"] == 0


def test_over_the_cap_nothing_is_downloaded_even_with_yes(script):
    fd, calls = script
    calls["cost"] = 25.0
    assert fd.main(["--name", "ES", "--yes"]) == 2
    assert calls["download"] == 0


def test_under_the_cap_with_yes_downloads_and_resamples(script, tmp_path):
    fd, calls = script
    assert fd.main(["--name", "ES", "--yes"]) == 0
    assert calls["download"] == 1
    assert (tmp_path / "data" / "csv" / "ES.csv").exists()
