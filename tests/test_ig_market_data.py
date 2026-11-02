"""
IG as a price source, and broker selection.

Two things here are easy to get silently wrong. First, IG quotes bid and ask
separately: building candles from one side bakes half the spread into every
indicator. Second, an IG-only setup used to fall through to CSV and scan
nothing at all, which looks like the bot simply finding no setups.
"""
from __future__ import annotations

import httpx
import pytest

from src.broker.ig import IGBroker
from src.broker.paper import SimulatedBroker
from src.broker.registry import build_broker
from src.data.market_data import (
    CSVMarketData,
    IGMarketData,
    MarketDataError,
    _ig_mid,
    _parse_ig_price,
    build_market_data,
)

from tests.test_ig_broker import FakeIG, build, make_settings


# ---------------------------------------------------------------------- #
# Price parsing
# ---------------------------------------------------------------------- #
def test_a_candle_is_built_from_the_mid_not_one_side():
    candle = _parse_ig_price(
        {
            "snapshotTimeUTC": "2026-03-02T00:00:00",
            "openPrice": {"bid": 99.0, "ask": 101.0},
            "highPrice": {"bid": 104.0, "ask": 106.0},
            "lowPrice": {"bid": 94.0, "ask": 96.0},
            "closePrice": {"bid": 102.0, "ask": 104.0},
            "lastTradedVolume": 1234,
        }
    )
    assert candle.open == 100.0
    assert candle.high == 105.0
    assert candle.low == 95.0
    assert candle.close == 103.0
    assert candle.volume == 1234.0
    assert candle.ts.tzinfo is not None


def test_a_one_sided_quote_uses_the_side_that_exists():
    """Out-of-hours bars can carry a null; returning 0.0 would poison the
    moving averages for the next 200 bars."""
    assert _ig_mid({"bid": None, "ask": 50.0}) == 50.0
    assert _ig_mid({"bid": 50.0, "ask": None}) == 50.0
    assert _ig_mid({"bid": None, "ask": None, "lastTraded": 42.0}) == 42.0
    assert _ig_mid(None) == 0.0
    assert _ig_mid({}) == 0.0


def test_igs_own_timestamp_format_is_accepted():
    candle = _parse_ig_price(
        {"snapshotTime": "2026/03/02 13:45:00", "closePrice": {"bid": 1.0, "ask": 1.0}}
    )
    assert candle.ts.year == 2026
    assert candle.ts.hour == 13
    assert candle.ts.tzinfo is not None


# ---------------------------------------------------------------------- #
# Fetching
# ---------------------------------------------------------------------- #
PRICES = {
    "prices": [
        {
            "snapshotTimeUTC": "2026-03-02T00:00:00",
            "openPrice": {"bid": 99.0, "ask": 101.0},
            "highPrice": {"bid": 104.0, "ask": 106.0},
            "lowPrice": {"bid": 94.0, "ask": 96.0},
            "closePrice": {"bid": 102.0, "ask": 104.0},
            "lastTradedVolume": 10,
        }
    ],
    "allowance": {"remainingAllowance": 9_000, "totalAllowance": 10_000},
}


def ig_data(tmp_path, fake: FakeIG, **kw) -> tuple[IGMarketData, IGBroker]:
    settings = make_settings(data_dir=str(tmp_path), ig_epics="FTSE=IX.D.FTSE.DAILY.IP", **kw)
    broker = build(fake, settings)
    return IGMarketData(settings, broker), broker


def test_candles_are_fetched_at_the_right_resolution(tmp_path):
    fake = FakeIG({"GET /prices/IX.D.FTSE.DAILY.IP": PRICES})
    data, _ = ig_data(tmp_path, fake)
    candles = data.get_candles("FTSE", "1Day", limit=50)

    assert len(candles) == 1
    assert candles[0].close == 103.0

    call = next(c for c in fake.calls if "/prices/" in str(c.url))
    assert call.headers["VERSION"] == "3"
    assert call.url.params["resolution"] == "DAY"
    assert call.url.params["max"] == "50"


@pytest.mark.parametrize(
    "timeframe,resolution",
    [("1Day", "DAY"), ("1Hour", "HOUR"), ("15Min", "MINUTE_15"), ("5Min", "MINUTE_5")],
)
def test_every_bot_timeframe_maps_to_an_ig_resolution(tmp_path, timeframe, resolution):
    fake = FakeIG({"GET /prices/IX.D.FTSE.DAILY.IP": PRICES})
    data, _ = ig_data(tmp_path, fake)
    data.get_candles("FTSE", timeframe)
    call = next(c for c in fake.calls if "/prices/" in str(c.url))
    assert call.url.params["resolution"] == resolution


def test_an_unknown_timeframe_is_rejected(tmp_path):
    data, _ = ig_data(tmp_path, FakeIG())
    with pytest.raises(ValueError, match="unsupported timeframe"):
        data.get_candles("FTSE", "1Week")


def test_bars_are_cached_so_the_weekly_allowance_survives(tmp_path):
    """
    IG meters historical prices against a weekly allowance. Re-fetching a daily
    bar on every scan would exhaust it in hours.
    """
    fake = FakeIG({"GET /prices/IX.D.FTSE.DAILY.IP": PRICES})
    data, _ = ig_data(tmp_path, fake)
    data.get_candles("FTSE", "1Day")
    data.get_candles("FTSE", "1Day")

    fetches = [c for c in fake.calls if "/prices/" in str(c.url)]
    assert len(fetches) == 1
    # Daily bars are held much longer than Alpaca's hour, on purpose.
    assert data.CACHE_TTL["1Day"] >= 6 * 3600


def test_an_exhausted_allowance_says_what_it_is(tmp_path):
    def prices(request):
        return httpx.Response(
            403, json={"errorCode": "error.public-api.exceeded-api-key-allowance"}
        )

    fake = FakeIG({"GET /prices/IX.D.FTSE.DAILY.IP": prices})
    data, _ = ig_data(tmp_path, fake)
    with pytest.raises(MarketDataError, match="allowance is exhausted"):
        data.get_candles("FTSE", "1Day")


def test_an_unresolvable_symbol_is_a_market_data_error(tmp_path):
    fake = FakeIG({"GET /markets": {"markets": []}})
    data, _ = ig_data(tmp_path, fake)
    with pytest.raises(MarketDataError, match="IG price fetch failed"):
        data.get_candles("NOSUCH", "1Day")


# ---------------------------------------------------------------------- #
# Wiring
# ---------------------------------------------------------------------- #
def test_an_ig_setup_gets_ig_prices_not_a_silent_csv_fallback(tmp_path):
    settings = make_settings(data_dir=str(tmp_path))
    broker = build(FakeIG(), settings)
    provider = build_market_data(settings, broker=broker)
    assert isinstance(provider, IGMarketData)


def test_ig_without_a_broker_falls_back_loudly_to_csv(tmp_path):
    settings = make_settings(data_dir=str(tmp_path))
    assert isinstance(build_market_data(settings), CSVMarketData)


def test_offline_mode_still_wins(tmp_path):
    settings = make_settings(data_dir=str(tmp_path))
    broker = build(FakeIG(), settings)
    assert isinstance(
        build_market_data(settings, force_csv=True, broker=broker), CSVMarketData
    )


def test_build_broker_picks_ig_when_its_keys_are_present(tmp_path):
    settings = make_settings(data_dir=str(tmp_path))
    broker = build_broker(settings)
    assert isinstance(broker, IGBroker)
    assert broker.name == "ig"
    broker.close()


def test_build_broker_respects_an_explicit_choice(tmp_path):
    settings = make_settings(data_dir=str(tmp_path), broker="simulated")
    broker = build_broker(settings)
    assert isinstance(broker, SimulatedBroker)


def test_force_simulated_overrides_ig(tmp_path):
    settings = make_settings(data_dir=str(tmp_path))
    assert isinstance(build_broker(settings, force_simulated=True), SimulatedBroker)
