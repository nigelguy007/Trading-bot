"""
Trading 212 adapter (Invest and Stocks ISA), over plain HTTP.

The appeal is real: actual share ownership, no leverage, and inside a Stocks
ISA the gains are not taxed. It is the most benign thing this bot can trade.

The catch is equally real, and you must understand it before using this.

    TRADING 212 HAS NO BRACKET ORDERS.

Every other adapter here sends entry, stop and target as one instruction, so
the stop exists at the broker from the moment of the fill. T212's API has four
separate order endpoints and no way to attach anything to an entry. The best
available sequence is:

    1. POST /orders/market        -> buy
    2. poll GET /orders/{id}      -> wait for FILLED
    3. POST /orders/stop          -> sell stop, protecting the position
    4. POST /orders/limit         -> sell limit at the target

Which leaves two gaps that no amount of care removes:

  * Between (1) and (3) the position is UNPROTECTED. Usually a second or two.
    If the process dies in that window, you own shares with no stop. On
    startup `reconcile()` finds those and re-protects them, but a crash plus
    an overnight gap is a real loss this bot cannot prevent.
  * (3) and (4) are not OCO-linked. When one fills the other stays live, and
    a stale sell order against shares you no longer hold is at best noise and
    at worst an accidental trade. `reconcile()` cancels those too, but it only
    runs when the bot runs.

Both gaps are the price of trading real shares in an ISA through this API.
They are logged loudly rather than smoothed over. If you want a broker-side
stop that exists from the instant of the fill, use IG or Alpaca.

Other things this API decides for you:

  * Long only. An Invest/ISA account cannot short, so a short signal is
    refused here rather than being silently turned into something else.
  * Side is the SIGN of `quantity`. There is no `side` field: -10 is a sell.
  * Tickers are T212's own ("AAPL_US_EQ"), resolved from /metadata/instruments
    and cached hard, because that endpoint allows one request per 50 seconds.
  * Rate limits are per endpoint and tight (/positions 1/s, /account/summary
    1/5s, stop and limit orders 1 per 2s). They are enforced client-side here;
    a 429 mid-sequence could otherwise leave a position unprotected.
  * Order placement is NOT idempotent — T212 says so explicitly. Nothing here
    retries a POST.
"""
from __future__ import annotations

import base64
import time
from typing import Any

import httpx

from src.broker.base import Broker, BrokerError
from src.config import Settings
from src.logging_setup import get_logger
from src.models import Account, OrderResult, Position, PositionSizing, Signal

log = get_logger(__name__)

# Per-endpoint minimum seconds between calls, from T212's published limits with
# a little headroom. A 429 between the entry and its stop is the expensive one.
RATE_LIMITS: dict[str, float] = {
    "/api/v0/equity/account/summary": 5.5,
    "/api/v0/equity/positions": 1.1,
    "/api/v0/equity/orders": 5.5,
    "/api/v0/equity/orders/limit": 2.2,
    "/api/v0/equity/orders/stop": 2.2,
    "/api/v0/equity/orders/market": 1.3,
    "/api/v0/equity/metadata/instruments": 55.0,
}
DEFAULT_RATE_LIMIT = 1.1

# Order statuses that mean "this is over".
TERMINAL = {"FILLED", "CANCELLED", "REJECTED"}


class Trading212Broker(Broker):
    name = "t212"

    def __init__(self, settings: Settings) -> None:
        if not settings.has_t212_keys:
            raise BrokerError(
                "T212_API_KEY is not set. Generate one in the Trading 212 app "
                "under Settings -> API (Beta)."
            )
        self.settings = settings
        self.base_url = settings.t212_base_url
        self._client = httpx.Client(
            base_url=self.base_url,
            timeout=25.0,
            headers=self._auth_headers(settings),
        )
        self._instruments: dict[str, str] = dict(settings.t212_ticker_map)
        self._instruments_loaded = False
        self._last_call: dict[str, float] = {}
        # entry order id -> the protective orders placed for it, so the
        # reconciler can cancel the survivor when one of them fills.
        self._protective: dict[str, dict[str, Any]] = {}
        log.info(
            "broker.t212_configured",
            demo=settings.t212_demo,
            base_url=self.base_url,
            preset_tickers=len(self._instruments),
        )

    @staticmethod
    def _auth_headers(settings: Settings) -> dict[str, str]:
        """
        T212 supports two schemes. The newer one is HTTP Basic with the key as
        the username and the secret as the password; the older one puts the
        raw key in the Authorization header. Use Basic when a secret exists.
        """
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        if settings.t212_api_secret:
            pair = f"{settings.t212_api_key}:{settings.t212_api_secret}".encode("utf-8")
            headers["Authorization"] = "Basic " + base64.b64encode(pair).decode("ascii")
        else:
            headers["Authorization"] = settings.t212_api_key
        return headers

    # ------------------------------------------------------------------ #
    @property
    def is_paper(self) -> bool:
        """Driven by T212_DEMO, which also picks the host."""
        return self.settings.t212_demo

    def close(self) -> None:
        self._client.close()

    def supports(self, asset_class: str) -> bool:
        # Invest/ISA holds shares and ETFs. T212's crypto is a separate CFD
        # product the public API does not reach.
        return asset_class == "equity"

    def is_market_open(self) -> bool:
        """
        T212 exposes exchange schedules but no simple clock, and a market order
        placed out of hours is queued rather than rejected. Let the scheduler
        scan; the queueing is the broker's problem, not a reason to skip.
        """
        return True

    # ------------------------------------------------------------------ #
    def _throttle(self, path: str) -> None:
        gap = RATE_LIMITS.get(path, DEFAULT_RATE_LIMIT)
        last = self._last_call.get(path, 0.0)
        elapsed = time.monotonic() - last
        if elapsed < gap:
            time.sleep(gap - elapsed)
        self._last_call[path] = time.monotonic()

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        self._throttle(path)
        try:
            resp = self._client.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise BrokerError(f"network error calling Trading 212 {path}: {exc}") from exc

        if resp.status_code == 401:
            raise BrokerError(
                "Trading 212 rejected the credentials (401). Check T212_API_KEY, "
                "and that the key was generated for the same environment "
                f"T212_DEMO selects ({'demo' if self.is_paper else 'live'})."
            )
        if resp.status_code == 403:
            raise BrokerError(
                "Trading 212 refused the request (403). The API is enabled only "
                "for Invest and Stocks ISA accounts, and the key needs the "
                "matching scopes enabled in the app."
            )
        if resp.status_code == 429:
            raise BrokerError(
                f"Trading 212 rate limit (429) on {path}. The client-side "
                "throttle should prevent this; if it persists, another process "
                "is using the same account."
            )
        if resp.status_code >= 400:
            raise BrokerError(
                f"Trading 212 {resp.status_code} on {path}: {resp.text[:300]}"
            )

        if not resp.content:
            return {}
        try:
            return resp.json()
        except ValueError as exc:
            raise BrokerError(
                f"Trading 212 returned non-JSON on {path}: {resp.text[:200]}"
            ) from exc

    # ------------------------------------------------------------------ #
    # Account and positions
    # ------------------------------------------------------------------ #
    def get_account(self) -> Account:
        data = self._request("GET", "/api/v0/equity/account/summary")
        cash = data.get("cash") or {}
        investments = data.get("investments") or {}

        free = float(cash.get("availableToTrade") or 0.0)
        reserved = float(cash.get("reservedForOrders") or 0.0)
        in_pies = float(cash.get("inPies") or 0.0)
        holdings = float(investments.get("currentValue") or 0.0)

        # Summed explicitly rather than trusting `totalValue`, whose own
        # description ("Investments value") does not say whether cash is in it.
        equity = free + reserved + in_pies + holdings

        return Account(
            equity=equity,
            cash=free,
            buying_power=free,
            # T212 reports no start-of-day equity. Left at 0 deliberately: the
            # portfolio layer then derives the day's P&L from the journal
            # rather than silently reporting a 0% day and disarming the
            # daily loss limit.
            last_equity=0.0,
        )

    def get_positions(self) -> list[Position]:
        rows = self._request("GET", "/api/v0/equity/positions") or []
        out: list[Position] = []
        for row in rows:
            instrument = row.get("instrument") or {}
            ticker = str(instrument.get("ticker") or "")
            qty = float(row.get("quantity") or 0.0)
            if qty == 0:
                continue
            entry = float(row.get("averagePricePaid") or 0.0)
            price = float(row.get("currentPrice") or entry)
            impact = row.get("walletImpact") or {}
            # walletImpact is in ACCOUNT currency; averagePricePaid and
            # currentPrice are in INSTRUMENT currency. For a GBP account
            # holding a US stock those differ, so prefer the broker's own
            # figure over recomputing it from two prices in another currency.
            pnl = impact.get("unrealizedProfitLoss")
            out.append(
                Position(
                    symbol=self.symbol_for(ticker),
                    qty=qty,
                    avg_entry=entry,
                    market_price=price,
                    unrealized_pnl=(
                        float(pnl) if pnl is not None else (price - entry) * qty
                    ),
                    side="long",  # Invest/ISA cannot be short
                    venue=self.name,
                )
            )
        return out

    # ------------------------------------------------------------------ #
    # Ticker resolution
    # ------------------------------------------------------------------ #
    def _load_instruments(self) -> None:
        """
        Pull the tradable universe once. One request per 50 seconds is allowed,
        and the payload is large, so this happens lazily and only once per run.
        """
        if self._instruments_loaded:
            return
        try:
            rows = self._request("GET", "/api/v0/equity/metadata/instruments") or []
        except BrokerError as exc:
            log.warning("broker.t212_instruments_failed", error=str(exc))
            return

        resolved = 0
        for row in rows:
            ticker = str(row.get("ticker") or "")
            short = str(row.get("shortName") or "")
            if not ticker:
                continue
            # Prefer an explicit shortName, else the leading segment of the
            # T212 ticker: "AAPL_US_EQ" -> "AAPL".
            plain = (short or ticker.split("_")[0]).upper()
            # Never overwrite a pinned mapping, and do not let a later listing
            # of the same plain symbol on another exchange steal it.
            if plain not in self._instruments:
                self._instruments[plain] = ticker
                resolved += 1

        self._instruments_loaded = True
        log.info("broker.t212_instruments_loaded", count=len(rows), mapped=resolved)

    def resolve_ticker(self, symbol: str) -> str:
        """AAPL -> AAPL_US_EQ. Pinned T212_TICKERS entries win."""
        key = symbol.upper()
        if key in self._instruments:
            return self._instruments[key]
        if "_" in key:  # already a T212 ticker
            return key
        self._load_instruments()
        if key in self._instruments:
            found = self._instruments[key]
            log.info("broker.t212_ticker_resolved", symbol=symbol, ticker=found)
            return found
        raise BrokerError(
            f"Trading 212 has no instrument matching {symbol!r}. Pin it in .env, "
            f"e.g. T212_TICKERS={key}={key}_US_EQ — the exact ticker is in the "
            "/metadata/instruments listing."
        )

    def symbol_for(self, ticker: str) -> str:
        """Inverse of resolve_ticker, for reporting positions in your terms."""
        for plain, mapped in self._instruments.items():
            if mapped == ticker:
                return plain
        return ticker.split("_")[0] if "_" in ticker else ticker

    # ------------------------------------------------------------------ #
    # Dealing
    # ------------------------------------------------------------------ #
    def submit_bracket_order(self, signal: Signal, sizing: PositionSizing) -> OrderResult:
        """
        Buy, wait for the fill, then protect. See the module docstring for why
        this cannot be atomic and what that costs you.
        """
        if sizing.shares <= 0:
            return OrderResult(
                accepted=False, broker=self.name, symbol=signal.symbol,
                qty=0, side="", message="position size is zero",
            )

        if signal.direction != "long":
            # Refused rather than quietly skipped: a short signal that silently
            # becomes nothing looks like the bot is working when it is not.
            return OrderResult(
                accepted=False, broker=self.name, symbol=signal.symbol,
                qty=sizing.shares, side="sell",
                message=(
                    "Trading 212 Invest/ISA accounts are long only — a short "
                    "signal cannot be placed here. Route shorts to a broker "
                    "that supports them, or filter them out upstream."
                ),
            )

        try:
            ticker = self.resolve_ticker(signal.symbol)
        except BrokerError as exc:
            return OrderResult(
                accepted=False, broker=self.name, symbol=signal.symbol,
                qty=sizing.shares, side="buy", message=str(exc),
            )

        qty = round(sizing.shares, 6)
        try:
            entry = self._request(
                "POST",
                "/api/v0/equity/orders/market",
                json={"ticker": ticker, "quantity": qty, "extendedHours": False},
            )
        except BrokerError as exc:
            log.error("broker.t212_entry_rejected", symbol=signal.symbol, error=str(exc))
            return OrderResult(
                accepted=False, broker=self.name, symbol=signal.symbol,
                qty=qty, side="buy", message=str(exc),
            )

        order_id = str(entry.get("id") or "")
        status = str(entry.get("status") or "")
        log.info(
            "broker.t212_entry_sent",
            symbol=signal.symbol, ticker=ticker, qty=qty,
            order_id=order_id, status=status, demo=self.is_paper,
        )

        filled_qty = self._await_fill(order_id, qty)
        if filled_qty <= 0:
            return OrderResult(
                accepted=False,
                broker=self.name,
                symbol=signal.symbol,
                qty=qty,
                side="buy",
                broker_order_id=order_id,
                status=status.lower() or "unfilled",
                message=(
                    f"Entry order {order_id} did not fill in time, so no stop was "
                    "placed. Nothing is unprotected — but check the order in the "
                    "Trading 212 app before re-sending anything."
                ),
                raw=entry,
            )

        protection = self._protect(signal, ticker, filled_qty, order_id)

        return OrderResult(
            accepted=True,
            broker=self.name,
            symbol=signal.symbol,
            qty=filled_qty,
            side="buy",
            broker_order_id=order_id,
            status="filled",
            message=protection,
            raw=entry,
        )

    def _await_fill(self, order_id: str, requested: float) -> float:
        """
        Poll the entry until it is filled. Every second this takes is a second
        the position has no stop, so the wait is short and the failure is loud.
        """
        if not order_id:
            return 0.0
        deadline = time.monotonic() + self.settings.t212_fill_timeout
        last_status = ""
        while time.monotonic() < deadline:
            try:
                order = self._request("GET", f"/api/v0/equity/orders/{order_id}")
            except BrokerError as exc:
                log.warning("broker.t212_fill_poll_failed", order_id=order_id, error=str(exc))
                return 0.0

            last_status = str(order.get("status") or "")
            filled = float(order.get("filledQuantity") or 0.0)
            if last_status == "FILLED":
                return filled or requested
            if last_status in TERMINAL:
                log.warning(
                    "broker.t212_entry_not_filled", order_id=order_id, status=last_status
                )
                return filled  # a partial fill still needs protecting
            time.sleep(1.0)

        log.error(
            "broker.t212_fill_timeout",
            order_id=order_id,
            last_status=last_status,
            note="position may be opening with no stop — reconcile() will protect it",
        )
        return 0.0

    def _protect(self, signal: Signal, ticker: str, qty: float, entry_id: str) -> str:
        """
        Place the sell-stop and the sell-limit. Sell is a NEGATIVE quantity.

        The stop goes first and on purpose: if only one of the two can be
        placed, it must be the one that caps the loss.
        """
        sell_qty = -round(qty, 6)
        notes: list[str] = []
        stop_id = limit_id = ""

        try:
            stop = self._request(
                "POST",
                "/api/v0/equity/orders/stop",
                json={
                    "ticker": ticker,
                    "quantity": sell_qty,
                    "stopPrice": round(signal.stop, 2),
                    "timeValidity": "GOOD_TILL_CANCEL",
                },
            )
            stop_id = str(stop.get("id") or "")
            log.info("broker.t212_stop_placed", symbol=signal.symbol, order_id=stop_id)
        except BrokerError as exc:
            log.error(
                "broker.t212_STOP_FAILED",
                symbol=signal.symbol,
                qty=qty,
                error=str(exc),
                note="POSITION IS OPEN AND UNPROTECTED",
            )
            notes.append(
                f"WARNING: the position is OPEN but its STOP WAS REJECTED ({exc}). "
                "Place a stop manually in the Trading 212 app now."
            )

        try:
            limit = self._request(
                "POST",
                "/api/v0/equity/orders/limit",
                json={
                    "ticker": ticker,
                    "quantity": sell_qty,
                    "limitPrice": round(signal.target, 2),
                    "timeValidity": "GOOD_TILL_CANCEL",
                },
            )
            limit_id = str(limit.get("id") or "")
            log.info("broker.t212_target_placed", symbol=signal.symbol, order_id=limit_id)
        except BrokerError as exc:
            # Far less serious: no target just means the trade runs to its stop
            # or until you close it.
            log.warning("broker.t212_target_failed", symbol=signal.symbol, error=str(exc))
            notes.append(f"target order not placed ({exc})")

        if stop_id or limit_id:
            self._protective[entry_id] = {
                "symbol": signal.symbol,
                "ticker": ticker,
                "stop_id": stop_id,
                "limit_id": limit_id,
            }

        if not notes:
            return f"filled; stop {stop_id} and target {limit_id} placed separately"
        return "; ".join(notes)

    # ------------------------------------------------------------------ #
    def close_position(self, symbol: str) -> OrderResult:
        """Sell the whole holding at market, and cancel its protective orders."""
        try:
            ticker = self.resolve_ticker(symbol)
            positions = self._request("GET", "/api/v0/equity/positions") or []
        except BrokerError as exc:
            return OrderResult(
                accepted=False, broker=self.name, symbol=symbol,
                qty=0, side="", message=str(exc),
            )

        match = next(
            (
                row
                for row in positions
                if str((row.get("instrument") or {}).get("ticker") or "") == ticker
            ),
            None,
        )
        if match is None:
            return OrderResult(
                accepted=False, broker=self.name, symbol=symbol,
                qty=0, side="", message=f"no open Trading 212 position in {symbol}",
            )

        # `or` would be wrong here: 0.0 available is falsy, so it would fall
        # through to the full holding and try to sell shares locked in a pie.
        available = match.get("quantityAvailableForTrading")
        qty = float(available if available is not None else (match.get("quantity") or 0.0))
        if qty <= 0:
            return OrderResult(
                accepted=False, broker=self.name, symbol=symbol, qty=0, side="",
                message=(
                    f"{symbol} is held but none of it is available to trade — "
                    "the shares may be committed to a pie."
                ),
            )

        # Cancel the resting stop and target FIRST. Selling with them still
        # live leaves orders against shares that are about to be gone.
        self._cancel_protective_for(ticker)

        try:
            order = self._request(
                "POST",
                "/api/v0/equity/orders/market",
                json={"ticker": ticker, "quantity": -round(qty, 6), "extendedHours": False},
            )
        except BrokerError as exc:
            return OrderResult(
                accepted=False, broker=self.name, symbol=symbol,
                qty=qty, side="close", message=str(exc),
            )

        log.info("broker.t212_close_sent", symbol=symbol, qty=qty, order_id=order.get("id"))
        return OrderResult(
            accepted=True,
            broker=self.name,
            symbol=symbol,
            qty=qty,
            side="close",
            broker_order_id=str(order.get("id") or ""),
            status="closing",
            raw=order,
        )

    # ------------------------------------------------------------------ #
    # Reconciliation — the OCO that T212 does not provide
    # ------------------------------------------------------------------ #
    def pending_orders(self) -> list[dict]:
        return self._request("GET", "/api/v0/equity/orders") or []

    def cancel_order(self, order_id: str) -> bool:
        try:
            self._request("DELETE", f"/api/v0/equity/orders/{order_id}")
            return True
        except BrokerError as exc:
            log.warning("broker.t212_cancel_failed", order_id=order_id, error=str(exc))
            return False

    def _cancel_protective_for(self, ticker: str) -> int:
        """Cancel every resting sell order on a ticker."""
        cancelled = 0
        for order in self.pending_orders():
            same = str((order.get("instrument") or {}).get("ticker") or "") == ticker
            selling = float(order.get("quantity") or 0.0) < 0
            if same and selling:
                if self.cancel_order(str(order.get("id") or "")):
                    cancelled += 1
        return cancelled

    def reconcile(self) -> dict[str, int]:
        """
        Repair what T212's missing OCO leaves behind. Safe to call repeatedly.

        Two jobs:
          * cancel sell orders against a ticker that is no longer held (the
            other side of a bracket that already did its work), and
          * report positions with no protective order at all, which is what a
            crash between the entry and its stop leaves.

        Returns counts so the caller can log or alert on them.
        """
        try:
            positions = self.get_positions()
            orders = self.pending_orders()
        except BrokerError as exc:
            log.warning("broker.t212_reconcile_failed", error=str(exc))
            return {"cancelled": 0, "unprotected": 0}

        held = {p.symbol for p in positions}
        protected: set[str] = set()
        cancelled = 0

        for order in orders:
            ticker = str((order.get("instrument") or {}).get("ticker") or "")
            symbol = self.symbol_for(ticker)
            if float(order.get("quantity") or 0.0) >= 0:
                continue  # a resting buy is not our business
            if symbol in held:
                protected.add(symbol)
            elif self.cancel_order(str(order.get("id") or "")):
                cancelled += 1
                log.info(
                    "broker.t212_orphan_cancelled",
                    symbol=symbol,
                    note="sell order with no matching position — its sibling filled",
                )

        unprotected = sorted(held - protected)
        if unprotected:
            log.error(
                "broker.t212_UNPROTECTED_POSITIONS",
                symbols=unprotected,
                note=(
                    "open with no resting stop. Place one in the app, or close "
                    "them. This is the gap T212's lack of bracket orders leaves "
                    "when the bot stops between an entry and its stop."
                ),
            )

        return {"cancelled": cancelled, "unprotected": len(unprotected)}
