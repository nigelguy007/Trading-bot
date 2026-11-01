"""
Price history.

Alpaca is the primary source (same account as the broker, so paper data and
paper fills agree). A CSV provider covers backtests and offline development so
the whole pipeline is runnable before any key exists.
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


def build_market_data(settings: Settings, force_csv: bool = False) -> MarketDataProvider:
    if settings.has_broker_keys and not force_csv:
        return AlpacaMarketData(settings)
    log.warning(
        "marketdata.csv_fallback",
        reason=(
            "offline mode requested — using data/csv/*.csv"
            if force_csv
            else "no Alpaca keys configured — using data/csv/*.csv"
        ),
    )
    return CSVMarketData(settings.data_path / "csv")
