"""
Alpaca adapter (paper and live), over plain HTTP.

Entries always go out as a bracket order — entry, stop loss and take profit
submitted together — so the stop exists at the broker from the moment the fill
happens, not whenever the bot next wakes up. If the process dies thirty seconds
after entry, the stop is still there.

Crypto is the exception: Alpaca does not accept bracket orders on crypto, so
those go out as a plain entry and the stop is tracked bot-side. That is a real
gap and the caller is warned about it rather than being left to discover it.
"""
from __future__ import annotations

import httpx

from src.broker.base import Broker, BrokerError
from src.config import Settings
from src.logging_setup import get_logger
from src.models import Account, OrderResult, Position, PositionSizing, Signal

log = get_logger(__name__)


class AlpacaBroker(Broker):
    name = "alpaca"

    def __init__(self, settings: Settings) -> None:
        if not settings.has_broker_keys:
            raise BrokerError("ALPACA_API_KEY / ALPACA_SECRET_KEY are not set.")
        self.settings = settings
        self.base_url = settings.broker_base_url
        self._client = httpx.Client(
            base_url=self.base_url,
            timeout=20.0,
            headers={
                "APCA-API-KEY-ID": settings.alpaca_api_key,
                "APCA-API-SECRET-KEY": settings.alpaca_secret_key,
                "accept": "application/json",
            },
        )
        log.info("broker.alpaca_ready", paper=self.is_paper, base_url=self.base_url)

    @property
    def is_paper(self) -> bool:
        return not self.settings.live_trading_enabled

    def close(self) -> None:
        self._client.close()

    # ------------------------------------------------------------------ #
    def _request(self, method: str, path: str, **kwargs) -> dict | list:
        try:
            resp = self._client.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise BrokerError(f"network error calling Alpaca {path}: {exc}") from exc

        if resp.status_code == 401:
            raise BrokerError(
                "Alpaca rejected the credentials (401). Check ALPACA_API_KEY / "
                "ALPACA_SECRET_KEY, and confirm they are PAPER keys if ALPACA_PAPER=true "
                "— paper and live keys are not interchangeable."
            )
        if resp.status_code == 403:
            raise BrokerError(f"Alpaca refused the request (403): {resp.text[:300]}")
        if resp.status_code == 422:
            raise BrokerError(
                f"Alpaca rejected the order (422): {resp.text[:300]}. Usual causes: "
                "bad symbol format, market closed, or fractional shares on an order "
                "type that doesn't allow them."
            )
        if resp.status_code == 429:
            raise BrokerError("Alpaca rate limit (429). Slow the scan loop down.")
        if resp.status_code >= 400:
            raise BrokerError(f"Alpaca {resp.status_code} on {path}: {resp.text[:300]}")

        return resp.json() if resp.content else {}

    # ------------------------------------------------------------------ #
    def get_account(self) -> Account:
        data = self._request("GET", "/v2/account")
        return Account(
            equity=float(data.get("equity", 0)),
            cash=float(data.get("cash", 0)),
            buying_power=float(data.get("buying_power", 0)),
            last_equity=float(data.get("last_equity", 0) or 0),
        )

    def get_positions(self) -> list[Position]:
        data = self._request("GET", "/v2/positions")
        positions = []
        for p in data or []:
            qty = float(p.get("qty", 0))
            positions.append(
                Position(
                    symbol=p.get("symbol", ""),
                    qty=qty,
                    avg_entry=float(p.get("avg_entry_price", 0)),
                    market_price=float(p.get("current_price", 0) or 0),
                    unrealized_pnl=float(p.get("unrealized_pl", 0) or 0),
                    side="long" if qty >= 0 else "short",
                )
            )
        return positions

    def is_market_open(self) -> bool:
        try:
            return bool(self._request("GET", "/v2/clock").get("is_open", False))
        except BrokerError as exc:
            log.warning("broker.clock_failed", error=str(exc))
            return False

    # ------------------------------------------------------------------ #
    def submit_bracket_order(self, signal: Signal, sizing: PositionSizing) -> OrderResult:
        if sizing.shares <= 0:
            return OrderResult(
                accepted=False, broker=self.name, symbol=signal.symbol,
                qty=0, side="", message="position size is zero",
            )

        side = "buy" if signal.direction == "long" else "sell"
        qty = sizing.shares
        payload: dict = {
            "symbol": signal.symbol,
            "qty": str(qty if signal.asset_class == "crypto" else int(qty)),
            "side": side,
            "type": "market",
            "time_in_force": "gtc" if signal.asset_class == "crypto" else "day",
        }

        if signal.asset_class == "crypto":
            log.warning(
                "broker.crypto_no_bracket",
                symbol=signal.symbol,
                note="Alpaca has no bracket orders for crypto; stop is tracked bot-side only",
            )
        else:
            payload.update(
                {
                    "order_class": "bracket",
                    "take_profit": {"limit_price": round(signal.target, 2)},
                    "stop_loss": {"stop_price": round(signal.stop, 2)},
                }
            )

        try:
            data = self._request("POST", "/v2/orders", json=payload)
        except BrokerError as exc:
            log.error("broker.order_rejected", symbol=signal.symbol, error=str(exc))
            return OrderResult(
                accepted=False, broker=self.name, symbol=signal.symbol,
                qty=qty, side=side, message=str(exc),
            )

        log.info(
            "broker.order_submitted",
            symbol=signal.symbol, side=side, qty=qty,
            order_id=data.get("id"), paper=self.is_paper,
        )
        return OrderResult(
            accepted=True,
            broker=self.name,
            symbol=signal.symbol,
            qty=qty,
            side=side,
            broker_order_id=str(data.get("id", "")),
            status=str(data.get("status", "")),
            raw=data if isinstance(data, dict) else {},
        )

    def close_position(self, symbol: str) -> OrderResult:
        try:
            data = self._request("DELETE", f"/v2/positions/{symbol.replace('/', '')}")
        except BrokerError as exc:
            return OrderResult(
                accepted=False, broker=self.name, symbol=symbol,
                qty=0, side="", message=str(exc),
            )
        return OrderResult(
            accepted=True,
            broker=self.name,
            symbol=symbol,
            qty=float(data.get("qty", 0) or 0) if isinstance(data, dict) else 0,
            side="close",
            broker_order_id=str(data.get("id", "")) if isinstance(data, dict) else "",
            raw=data if isinstance(data, dict) else {},
        )
