"""
Price history.

Alpaca is the primary source (same account as the broker, so paper data and
paper fills agree). IG serves the same role for a UK spread betting account.
A CSV provider covers backtests and offline development so the whole pipeline
is runnable before any key exists.

One warning about IG as a data source, because it will bite otherwise: IG meters
historical price requests against a WEEKLY allowance (10,000 data points on a
live account; demo is more generous). A 250-bar daily history for eight symbols
is 2,000 points — four full refreshes and the week is gone. Daily bars are
therefore cached for six hours rather than one, the remaining allowance is
logged, and an exhausted allowance is reported as itself instead of as a
mystery 403. Keep WATCHLIST short on IG.
"""
from __future__ import annotations

import csv
from abc import ABC, abstractmethod
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

from src.config import Settings
from src.data.cache import TTLCache
from src.logging_setup import get_logger
from src.models import Candle

log = get_logger(__name__)

ALPACA_DATA_URL = "https://data.alpaca.markets"

# Alpaca timeframe strings keyed by the interval names the bot uses.
TIMEFRAMES = {"1Day": "1Day", "1Hour": "1Hour", "15Min": "15Min", "5Min": "5Min"}


class MarketDataError(RuntimeError):
    pass


class MarketDataProvider(ABC):
    @abstractmethod
    def get_candles(self, symbol: str, timeframe: str = "1Day", limit: int = 250) -> list[Candle]:
        ...

    def get_last_price(self, symbol: str) -> float:
        candles = self.get_candles(symbol, "1Day", limit=2)
        if not candles:
            raise MarketDataError(f"no price data for {symbol}")
        return candles[-1].close


def _is_crypto(symbol: str) -> bool:
    return "/" in symbol


def _parse_bar(bar: dict) -> Candle:
    return Candle(
        ts=datetime.fromisoformat(bar["t"].replace("Z", "+00:00")),
        open=float(bar["o"]),
        high=float(bar["h"]),
        low=float(bar["l"]),
        close=float(bar["c"]),
        volume=float(bar.get("v", 0)),
    )


def _ig_mid(price: dict | None) -> float:
    """
    Mid of an IG bid/ask pair, falling back to whichever side exists.

    Some IG markets return only `lastTraded`, and out-of-hours bars can carry a
    null on one side; returning 0.0 there would poison every indicator, so the
    available side is used instead.
    """
    if not price:
        return 0.0
    bid, ask = price.get("bid"), price.get("ask")
    if bid is not None and ask is not None:
        return (float(bid) + float(ask)) / 2.0
    for value in (bid, ask, price.get("lastTraded")):
        if value is not None:
            return float(value)
    return 0.0


def _parse_ig_price(row: dict) -> Candle:
    ts = str(row.get("snapshotTimeUTC") or row.get("snapshotTime") or "")
    # snapshotTimeUTC is ISO without a zone; snapshotTime uses IG's own
    # "yyyy/MM/dd HH:mm:ss" in the account's timezone.
    try:
        parsed = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        parsed = datetime.strptime(ts, "%Y/%m/%d %H:%M:%S")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)

    return Candle(
        ts=parsed,
        open=_ig_mid(row.get("openPrice")),
        high=_ig_mid(row.get("highPrice")),
        low=_ig_mid(row.get("lowPrice")),
        close=_ig_mid(row.get("closePrice")),
        volume=float(row.get("lastTradedVolume") or 0),
    )


class AlpacaMarketData(MarketDataProvider):
    """Alpaca market data v2 (equities) and v1beta3 (crypto), over plain HTTP."""

    # How long a bar of each size stays fresh. A daily bar doesn't change
    # intraday, so re-fetching it every scan is pure rate-limit burn.
    CACHE_TTL = {"1Day": 3600, "1Hour": 900, "15Min": 300, "5Min": 120}

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.cache = TTLCache(settings.data_path, "bars")
        self._client = httpx.Client(
            timeout=20.0,
            headers={
                "APCA-API-KEY-ID": settings.alpaca_api_key,
                "APCA-API-SECRET-KEY": settings.alpaca_secret_key,
                "accept": "application/json",
            },
        )

    def close(self) -> None:
        self._client.close()

    def get_candles(self, symbol: str, timeframe: str = "1Day", limit: int = 250) -> list[Candle]:
        if timeframe not in TIMEFRAMES:
            raise ValueError(f"unsupported timeframe {timeframe!r}; use one of {list(TIMEFRAMES)}")

        key = f"{symbol}|{timeframe}|{limit}|{self.settings.alpaca_data_feed}"
        ttl = self.CACHE_TTL.get(timeframe, 300)
        raw = self.cache.get(key, ttl)
        if raw is None:
            raw = self._fetch(symbol, timeframe, limit)
            if raw:
                self.cache.set(key, raw)
        return [_parse_bar(b) for b in raw or []]

    # -- internals ------------------------------------------------------- #
    def _fetch(self, symbol: str, timeframe: str, limit: int) -> list[dict]:
        # Ask for enough calendar span to actually contain `limit` bars.
        span_days = {"1Day": limit * 2, "1Hour": limit // 5 + 10, "15Min": limit // 26 + 5}
        start = datetime.now(timezone.utc) - timedelta(days=max(span_days.get(timeframe, 30), 5))

        if _is_crypto(symbol):
            url = f"{ALPACA_DATA_URL}/v1beta3/crypto/us/bars"
            params = {
                "symbols": symbol,
                "timeframe": timeframe,
                "start": start.date().isoformat(),
                "limit": limit,
            }
        else:
            url = f"{ALPACA_DATA_URL}/v2/stocks/bars"
            params = {
                "symbols": symbol,
                "timeframe": timeframe,
                "start": start.date().isoformat(),
                "limit": limit,
                "adjustment": "split",
                "feed": self.settings.alpaca_data_feed,
            }

        try:
            resp = self._client.get(url, params=params)
        except httpx.HTTPError as exc:
            raise MarketDataError(f"network error fetching {symbol}: {exc}") from exc

        if resp.status_code == 401:
            raise MarketDataError(
                "Alpaca rejected the credentials (401). Check ALPACA_API_KEY / "
                "ALPACA_SECRET_KEY in .env for typos or trailing spaces."
            )
        if resp.status_code == 429:
            raise MarketDataError(
                "Alpaca rate limit (429). Increase SCAN_INTERVAL_MINUTES or shorten WATCHLIST."
            )
        if resp.status_code >= 400:
            raise MarketDataError(f"Alpaca {resp.status_code} for {symbol}: {resp.text[:200]}")

        bars = (resp.json().get("bars") or {}).get(symbol, [])
        if not bars:
            log.warning("marketdata.no_bars", symbol=symbol, timeframe=timeframe)
        return bars


class IGMarketData(MarketDataProvider):
    """
    IG historical prices, through the broker's existing session.

    Reuses the IGBroker rather than opening its own session: epic resolution,
    the CST/token pair and the request throttle all already live there, and a
    second concurrent session would just double the rate-limit pressure.

    IG quotes bid and ask separately. Candles are built from the mid, which is
    what indicators should see — using the bid for a long entry would bake half
    the spread into every signal.
    """

    # Longer than Alpaca's on purpose: see the allowance note at the top.
    CACHE_TTL = {"1Day": 21_600, "1Hour": 1_800, "15Min": 600, "5Min": 240}

    # bot timeframe -> IG resolution
    RESOLUTIONS = {
        "1Day": "DAY",
        "1Hour": "HOUR",
        "15Min": "MINUTE_15",
        "5Min": "MINUTE_5",
    }

    def __init__(self, settings: Settings, broker) -> None:
        self.settings = settings
        self.broker = broker
        self.cache = TTLCache(settings.data_path, "ig-bars")

    def close(self) -> None:
        # The broker owns the HTTP client and is closed by the app's teardown.
        return None

    def get_candles(self, symbol: str, timeframe: str = "1Day", limit: int = 250) -> list[Candle]:
        resolution = self.RESOLUTIONS.get(timeframe)
        if resolution is None:
            raise ValueError(
                f"unsupported timeframe {timeframe!r}; use one of {list(self.RESOLUTIONS)}"
            )

        key = f"ig|{symbol}|{timeframe}|{limit}"
        raw = self.cache.get(key, self.CACHE_TTL.get(timeframe, 1_800))
        if raw is None:
            raw = self._fetch(symbol, resolution, limit)
            if raw:
                self.cache.set(key, raw)
        return [_parse_ig_price(p) for p in raw or []]

    # -- internals ------------------------------------------------------- #
    def _fetch(self, symbol: str, resolution: str, limit: int) -> list[dict]:
        from src.broker.base import BrokerError

        try:
            epic = self.broker.resolve_epic(symbol)
            payload = self.broker._request(
                "GET",
                f"/prices/{epic}",
                version="3",
                params={"resolution": resolution, "max": limit, "pageSize": 0},
            )
        except BrokerError as exc:
            message = str(exc)
            if "allowance" in message.lower():
                raise MarketDataError(
                    "IG's historical price allowance is exhausted. It resets weekly. "
                    "Shorten WATCHLIST, raise SCAN_INTERVAL_MINUTES, or use a demo "
                    f"account for testing. ({message})"
                ) from exc
            raise MarketDataError(f"IG price fetch failed for {symbol}: {message}") from exc

        allowance = payload.get("allowance") or {}
        remaining = allowance.get("remainingAllowance")
        if isinstance(remaining, (int, float)):
            total = allowance.get("totalAllowance") or 0
            if remaining <= 0:
                log.error(
                    "marketdata.ig_allowance_exhausted",
                    expiry_seconds=allowance.get("allowanceExpiry"),
                )
            elif total and remaining < total * 0.2:
                log.warning(
                    "marketdata.ig_allowance_low",
                    remaining=remaining,
                    total=total,
                    note="shorten WATCHLIST or raise SCAN_INTERVAL_MINUTES",
                )

        prices = payload.get("prices") or []
        if not prices:
            log.warning("marketdata.no_bars", symbol=symbol, epic=epic, source="ig")
        return prices


class CSVMarketData(MarketDataProvider):
    """
    Reads data/csv/<SYMBOL>.csv with a ts,open,high,low,close,volume header.

    Lets you backtest and exercise the whole pipeline with zero API keys.
    """

    def __init__(self, directory: Path) -> None:
        self.dir = directory
        self.dir.mkdir(parents=True, exist_ok=True)

    def get_candles(self, symbol: str, timeframe: str = "1Day", limit: int = 250) -> list[Candle]:
        path = self.dir / f"{symbol.replace('/', '-')}.csv"
        if not path.exists():
            raise MarketDataError(f"no CSV for {symbol} at {path}")

        candles: list[Candle] = []
        with path.open(newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                ts = row.get("ts") or row.get("timestamp") or row.get("date") or ""
                candles.append(
                    Candle(
                        ts=datetime.fromisoformat(ts.replace("Z", "+00:00")),
                        open=float(row["open"]),
                        high=float(row["high"]),
                        low=float(row["low"]),
                        close=float(row["close"]),
                        volume=float(row.get("volume", 0) or 0),
                    )
                )
        candles.sort(key=lambda c: c.ts)
        return candles[-limit:]


def build_market_data(
    settings: Settings, force_csv: bool = False, broker=None
) -> MarketDataProvider:
    """
    Pick a price source.

    `broker` is the already-constructed broker, passed in so an IG setup can
    read prices through the same session it deals on. Without it an IG-only
    configuration would silently fall back to CSV and the bot would scan
    nothing, which is the kind of quiet failure that wastes an afternoon.
    """
    if not force_csv:
        if settings.active_broker == "ig":
            if broker is not None and hasattr(broker, "resolve_epic"):
                return IGMarketData(settings, broker)
            log.warning(
                "marketdata.ig_without_broker",
                note="IG is configured but no IG broker was supplied — falling back to CSV",
            )
        elif settings.has_broker_keys:
            return AlpacaMarketData(settings)

    log.warning(
        "marketdata.csv_fallback",
        reason=(
            "offline mode requested — using data/csv/*.csv"
            if force_csv
            else "no broker price source configured — using data/csv/*.csv"
        ),
    )
    return CSVMarketData(settings.data_path / "csv")
