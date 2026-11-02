"""
IG adapter (IG Index / IG Markets, UK), over plain HTTP.

READ THIS BEFORE USING IT.

IG is not a share dealer in the sense the rest of this bot assumes. An IG demo
account — and most IG retail accounts — trades spread bets and CFDs, which are
LEVERAGED DERIVATIVES:

  * You never own the instrument. You take a position on its price.
  * Size is a stake in currency per point, not a number of shares. "2.5" on
    a FTSE market means £2.50 profit or loss per index point.
  * £500 of margin can carry £10,000 of exposure. A 5% adverse move is your
    whole deposit, not 5% of it.
  * Losses can exceed the deposit on a CFD account. IG applies negative
    balance protection for UK retail clients, but gaps through a stop still
    lose far more than the planned risk on a geared position.
  * Spread bet profits are currently free of UK CGT and stamp duty; CFD
    profits are not. Tax treatment depends on your circumstances and changes.
    That is not advice, and it is not a reason to prefer gearing.

The risk gate upstream still sizes every trade to risk at most
RISK_PER_TRADE_PCT of equity *if the stop fills at its level*. Gearing does not
change that arithmetic — it changes what happens when the stop does not fill at
its level, which on a geared position is materially worse. Use a guaranteed
stop (IG charges a premium) if that tail matters to you.

Mechanics this adapter relies on, all from IG's REST API:

  POST /session               VERSION 2  -> CST + X-SECURITY-TOKEN headers
  GET  /accounts              VERSION 1  -> balance, deposit, available
  GET  /positions             VERSION 2  -> open positions
  POST /positions/otc         VERSION 2  -> opens a deal, returns dealReference
  GET  /confirms/{dealRef}    VERSION 1  -> what actually happened to it
  POST /positions/otc         VERSION 1  -> closes one, with header _method: DELETE
  GET  /markets?searchTerm=   VERSION 1  -> epic lookup
  GET  /markets/{epic}        VERSION 3  -> dealing rules, snapshot

The one awkward bit is that IG's "accept the deal" call is asynchronous: the
POST returns a dealReference, and you then fetch /confirms to find out whether
it was accepted, rejected, or filled at a different level. An unconfirmed deal
is not the same as a failed one, so a confirm that cannot be read is reported
as accepted-but-unconfirmed rather than as a failure, and never silently
retried: retrying an order you cannot see is how you end up with two.
"""
from __future__ import annotations

import time
from typing import Any

import httpx

from src.broker.base import Broker, BrokerError
from src.config import Settings
from src.logging_setup import get_logger
from src.models import Account, OrderResult, Position, PositionSizing, Signal

log = get_logger(__name__)

# Non-expiring markets (spread bets on cash/daily markets) use "-".
NON_EXPIRING = "-"

# IG error codes worth translating. Anything else is passed through verbatim.
ERROR_HINTS = {
    "error.security.api-key-invalid": (
        "IG rejected the API key. Demo and live keys are NOT interchangeable: a "
        "demo key only works against demo-api.ig.com. Check IG_DEMO matches the "
        "key you generated (My IG -> Settings -> API keys)."
    ),
    "error.security.invalid-details": (
        "IG rejected the username or password. Note these are your IG WEB login "
        "details, not the API key, and the username is case-sensitive."
    ),
    "error.security.account-token-invalid": (
        "IG session token is invalid or expired. The adapter re-authenticates "
        "automatically; if this persists, the API key may have been revoked."
    ),
    "error.security.account-token-missing": "IG session was not established.",
    "error.public-api.failure.encryption.required": (
        "This IG account requires encrypted password login, which this adapter "
        "does not implement. Turn off 'Encrypted passwords' for the API key in "
        "My IG -> Settings -> API keys."
    ),
    "error.public-api.exceeded-api-key-allowance": (
        "IG rate limit on the API key. Raise SCAN_INTERVAL_MINUTES or "
        "IG_MIN_REQUEST_INTERVAL."
    ),
    "error.public-api.exceeded-account-allowance": (
        "IG rate limit on the account. Raise SCAN_INTERVAL_MINUTES."
    ),
    "error.public-api.exceeded-account-trading-allowance": (
        "IG trading-request rate limit on the account. The bot is placing deals "
        "too fast."
    ),
    "error.security.oauth-token-invalid": "IG OAuth token invalid.",
    "error.confirms.deal-not-found": (
        "IG has no confirmation for that deal reference yet. The deal may still "
        "be working — check your IG account before re-sending anything."
    ),
}


class IGBroker(Broker):
    name = "ig"

    def __init__(self, settings: Settings) -> None:
        if not settings.has_ig_keys:
            raise BrokerError(
                "IG_API_KEY / IG_USERNAME / IG_PASSWORD are not all set. "
                "Generate an API key in My IG -> Settings -> API keys."
            )
        self.settings = settings
        self.base_url = settings.ig_base_url
        self._client = httpx.Client(
            base_url=self.base_url,
            timeout=25.0,
            headers={
                "X-IG-API-KEY": settings.ig_api_key,
                "Accept": "application/json; charset=UTF-8",
                "Content-Type": "application/json; charset=UTF-8",
            },
        )
        self._cst: str = ""
        self._security_token: str = ""
        self._account_id: str = settings.ig_account_id
        self._account_type: str = ""
        self._currency: str = settings.ig_currency
        self._epics: dict[str, str] = dict(settings.ig_epic_map)
        self._last_request = 0.0
        log.info(
            "broker.ig_configured",
            demo=settings.ig_demo,
            base_url=self.base_url,
            preset_epics=len(self._epics),
        )

    # ------------------------------------------------------------------ #
    # Session
    # ------------------------------------------------------------------ #
    @property
    def is_paper(self) -> bool:
        """
        A demo account is the only thing here that cannot lose real money.

        Deliberately driven by IG_DEMO and not by the host URL: if someone
        points IG_DEMO=false at the live host, this must read False even before
        the first request proves it.
        """
        return self.settings.ig_demo

    def close(self) -> None:
        self._client.close()

    @property
    def authenticated(self) -> bool:
        return bool(self._cst and self._security_token)

    def login(self) -> None:
        """Exchange credentials for the CST / X-SECURITY-TOKEN pair."""
        self._throttle()
        try:
            resp = self._client.post(
                "/session",
                headers={"VERSION": "2"},
                json={
                    "identifier": self.settings.ig_username,
                    "password": self.settings.ig_password,
                },
            )
        except httpx.HTTPError as exc:
            raise BrokerError(f"network error reaching IG at {self.base_url}: {exc}") from exc

        if resp.status_code >= 400:
            raise BrokerError(f"IG login failed ({resp.status_code}): {_explain(resp)}")

        self._cst = resp.headers.get("CST", "")
        self._security_token = resp.headers.get("X-SECURITY-TOKEN", "")
        if not self.authenticated:
            raise BrokerError(
                "IG accepted the login but returned no session tokens. "
                "This usually means the API key belongs to a different "
                "environment than the one IG_DEMO selects."
            )

        body = resp.json() if resp.content else {}
        accounts = body.get("accounts") or []
        wanted = self._account_id or body.get("currentAccountId", "")
        chosen = next((a for a in accounts if a.get("accountId") == wanted), None)
        if chosen is None and accounts:
            chosen = accounts[0]
            if wanted:
                log.warning(
                    "broker.ig_account_not_found",
                    requested=wanted,
                    using=chosen.get("accountId"),
                    available=[a.get("accountId") for a in accounts],
                )
        if chosen:
            self._account_id = chosen.get("accountId", self._account_id)
            self._account_type = str(chosen.get("accountType", ""))
            self._currency = str(chosen.get("currency") or self._currency)

        log.info(
            "broker.ig_logged_in",
            account=self._account_id,
            account_type=self._account_type or "unknown",
            currency=self._currency,
            demo=self.is_paper,
        )
        if self._account_type and self._account_type.upper() not in {"SPREADBET", "CFD"}:
            log.warning(
                "broker.ig_unexpected_account_type",
                account_type=self._account_type,
                note="sizes are computed as stake-per-point; a share dealing "
                     "account would need a different sizing unit",
            )

    def _auth_headers(self, version: str) -> dict[str, str]:
        return {
            "VERSION": version,
            "CST": self._cst,
            "X-SECURITY-TOKEN": self._security_token,
        }

    def _throttle(self) -> None:
        """
        Keep a floor between calls. IG's allowances are per-minute and a scan
        over a watchlist bursts; a 429 mid-cycle is worse than being slow.
        """
        gap = self.settings.ig_min_request_interval
        if gap <= 0:
            return
        elapsed = time.monotonic() - self._last_request
        if elapsed < gap:
            time.sleep(gap - elapsed)
        self._last_request = time.monotonic()

    # ------------------------------------------------------------------ #
    def _request(
        self,
        method: str,
        path: str,
        *,
        version: str = "1",
        extra_headers: dict[str, str] | None = None,
        _retried: bool = False,
        **kwargs: Any,
    ) -> dict:
        if not self.authenticated:
            self.login()

        headers = self._auth_headers(version)
        if extra_headers:
            headers.update(extra_headers)

        self._throttle()
        try:
            resp = self._client.request(method, path, headers=headers, **kwargs)
        except httpx.HTTPError as exc:
            raise BrokerError(f"network error calling IG {path}: {exc}") from exc

        # A token expires on its own schedule. Re-authenticate once and retry —
        # but only for reads and only once, so a rejected order is never
        # resubmitted behind the caller's back.
        if resp.status_code in (401, 403) and not _retried and method.upper() == "GET":
            log.info("broker.ig_reauth", path=path, status=resp.status_code)
            self._cst = self._security_token = ""
            return self._request(
                method, path, version=version, extra_headers=extra_headers,
                _retried=True, **kwargs
            )

        if resp.status_code == 429:
            raise BrokerError(
                "IG rate limit (429). Raise SCAN_INTERVAL_MINUTES or "
                "IG_MIN_REQUEST_INTERVAL."
            )
        if resp.status_code >= 400:
            raise BrokerError(f"IG {resp.status_code} on {path}: {_explain(resp)}")

        if not resp.content:
            return {}
        try:
            data = resp.json()
        except ValueError as exc:
            raise BrokerError(f"IG returned non-JSON on {path}: {resp.text[:200]}") from exc
        return data if isinstance(data, dict) else {"data": data}

    # ------------------------------------------------------------------ #
    # Account and positions
    # ------------------------------------------------------------------ #
    def get_account(self) -> Account:
        data = self._request("GET", "/accounts", version="1")
        accounts = data.get("accounts") or []
        if not accounts:
            raise BrokerError("IG returned no accounts for these credentials.")

        chosen = next(
            (a for a in accounts if a.get("accountId") == self._account_id), accounts[0]
        )
        balance = chosen.get("balance") or {}
        equity = float(balance.get("balance") or 0.0)
        pnl = float(balance.get("profitLoss") or 0.0)
        available = float(balance.get("available") or 0.0)
        self._currency = str(chosen.get("currency") or self._currency)

        # IG reports `balance` as closed-trade equity and `profitLoss` as the
        # open-position mark separately. Equity here must include open P&L, or
        # a losing open book would never trip the daily loss limit.
        return Account(
            equity=equity + pnl,
            cash=float(balance.get("available") or 0.0),
            buying_power=available,
            # IG has no "equity at the open" field. day_pnl therefore means
            # open-position P&L, which is what the daily stop should watch:
            # positions closed earlier today are already in `balance`.
            last_equity=equity,
        )

    def get_positions(self) -> list[Position]:
        data = self._request("GET", "/positions", version="2")
        out: list[Position] = []
        for row in data.get("positions") or []:
            pos = row.get("position") or {}
            market = row.get("market") or {}
            size = float(pos.get("dealSize") or pos.get("size") or 0.0)
            direction = str(pos.get("direction", "BUY")).upper()
            level = float(pos.get("level") or 0.0)
            bid = float(market.get("bid") or 0.0)
            offer = float(market.get("offer") or 0.0)
            # Mark a long against the bid and a short against the offer: that
            # is the price you would actually get out at.
            mark = (bid if direction == "BUY" else offer) or level
            signed = size if direction == "BUY" else -size
            out.append(
                Position(
                    symbol=str(market.get("epic") or pos.get("dealId") or ""),
                    qty=signed,
                    avg_entry=level,
                    market_price=mark,
                    unrealized_pnl=(mark - level) * signed,
                    side="long" if direction == "BUY" else "short",
                )
            )
        return out

    def position_deal_ids(self) -> dict[str, str]:
        """epic -> dealId, needed to close a position (IG closes by dealId)."""
        data = self._request("GET", "/positions", version="2")
        mapping: dict[str, str] = {}
        for row in data.get("positions") or []:
            pos = row.get("position") or {}
            epic = str((row.get("market") or {}).get("epic") or "")
            deal_id = str(pos.get("dealId") or "")
            if epic and deal_id:
                mapping[epic] = deal_id
        return mapping

    def is_market_open(self) -> bool:
        """
        IG has no account-wide clock — every market has its own hours. The
        first configured epic stands in for "is there anything to scan".

        Permissive when it cannot tell: a needless scan costs an API call,
        whereas a false 'closed' silently stops the bot for a day. The real
        guard is that IG rejects a deal on a closed market.
        """
        epic = next(iter(self._epics.values()), "")
        if not epic:
            return True
        try:
            snapshot = self._request("GET", f"/markets/{epic}", version="3").get("snapshot") or {}
        except BrokerError as exc:
            log.warning("broker.ig_market_status_failed", epic=epic, error=str(exc))
            return True
        status = str(snapshot.get("marketStatus", "")).upper()
        return status in ("TRADEABLE", "")

    def supports(self, asset_class: str) -> bool:
        # IG covers equities, indices, FX and commodities as spread bets. It
        # does not do spot crypto, and its crypto derivatives are not available
        # to UK retail clients at all.
        return asset_class == "equity"

    # ------------------------------------------------------------------ #
    # Epic resolution
    # ------------------------------------------------------------------ #
    def resolve_epic(self, symbol: str) -> str:
        """
        Map a watchlist symbol to an IG epic, e.g. AAPL -> UA.D.AAPL.CASH.IP.

        Explicit IG_EPICS entries win. Search is the fallback and is cached,
        because IG's search ranks by its own relevance and the top hit for a
        bare ticker is not always the market you meant — which is exactly why
        a resolved epic is logged loudly the first time it is used.
        """
        key = symbol.upper()
        if key in self._epics:
            return self._epics[key]

        data = self._request("GET", "/markets", version="1", params={"searchTerm": symbol})
        markets = data.get("markets") or []
        if not markets:
            raise BrokerError(
                f"IG has no market matching {symbol!r}. Map it explicitly in .env, "
                f"e.g. IG_EPICS={key}=UA.D.{key}.CASH.IP — you can read the epic off "
                "the URL of the market on IG's own platform."
            )

        # Prefer a non-expiring (cash/daily) market: a dated future is a
        # different instrument with a different price and an expiry date.
        cash = [m for m in markets if str(m.get("expiry") or NON_EXPIRING) == NON_EXPIRING]
        chosen = (cash or markets)[0]
        epic = str(chosen.get("epic") or "")
        if not epic:
            raise BrokerError(f"IG returned a market for {symbol!r} with no epic.")

        self._epics[key] = epic
        log.warning(
            "broker.ig_epic_guessed",
            symbol=symbol,
            epic=epic,
            instrument=chosen.get("instrumentName"),
            candidates=len(markets),
            note="verify this is the market you meant, then pin it in IG_EPICS",
        )
        return epic

    def dealing_rules(self, epic: str) -> dict:
        """Minimum stake and minimum stop distance for a market, as IG states them."""
        data = self._request("GET", f"/markets/{epic}", version="3")
        rules = data.get("dealingRules") or {}
        snapshot = data.get("snapshot") or {}
        return {
            "min_deal_size": float((rules.get("minDealSize") or {}).get("value") or 0.0),
            "min_stop_distance": float(
                (rules.get("minNormalStopOrLimitDistance") or {}).get("value") or 0.0
            ),
            "min_stop_unit": str((rules.get("minNormalStopOrLimitDistance") or {}).get("unit", "")),
            "market_status": str(snapshot.get("marketStatus", "")),
            "scaling_factor": float(snapshot.get("scalingFactor") or 1.0),
            "currency": self._currency,
        }

    # ------------------------------------------------------------------ #
    # Dealing
    # ------------------------------------------------------------------ #
    def submit_bracket_order(self, signal: Signal, sizing: PositionSizing) -> OrderResult:
        """
        Open a position with its stop and limit attached to the same deal.

        IG does this in one request rather than as three linked orders, which
        is better than Alpaca's bracket: there is no window in which the entry
        exists without the stop. `forceOpen` is required for that — without it
        IG may net the deal against an existing position and drop the levels.
        """
        if sizing.shares <= 0:
            return OrderResult(
                accepted=False, broker=self.name, symbol=signal.symbol,
                qty=0, side="", message="position size is zero",
            )

        side = "BUY" if signal.direction == "long" else "SELL"

        try:
            epic = self.resolve_epic(signal.symbol)
        except BrokerError as exc:
            return OrderResult(
                accepted=False, broker=self.name, symbol=signal.symbol,
                qty=sizing.shares, side=side.lower(), message=str(exc),
            )

        payload = {
            "epic": epic,
            "expiry": NON_EXPIRING,
            "direction": side,
            "size": round(sizing.shares, 2),
            "orderType": "MARKET",
            "guaranteedStop": False,
            "stopLevel": round(signal.stop, 2),
            "limitLevel": round(signal.target, 2),
            # Without forceOpen, IG may offset an existing position in the same
            # market and discard stopLevel/limitLevel entirely.
            "forceOpen": True,
            "currencyCode": self._currency,
        }

        try:
            data = self._request(
                "POST", "/positions/otc", version="2", json=payload
            )
        except BrokerError as exc:
            log.error("broker.ig_deal_rejected", symbol=signal.symbol, epic=epic, error=str(exc))
            return OrderResult(
                accepted=False, broker=self.name, symbol=signal.symbol,
                qty=sizing.shares, side=side.lower(), message=str(exc),
            )

        deal_ref = str(data.get("dealReference") or "")
        if not deal_ref:
            return OrderResult(
                accepted=False, broker=self.name, symbol=signal.symbol,
                qty=sizing.shares, side=side.lower(),
                message="IG accepted the request but returned no dealReference.",
                raw=data,
            )

        return self._confirm(signal, sizing, side, epic, deal_ref)

    def _confirm(
        self,
        signal: Signal,
        sizing: PositionSizing,
        side: str,
        epic: str,
        deal_ref: str,
    ) -> OrderResult:
        """
        Resolve a dealReference into what actually happened.

        A confirm that cannot be read is NOT reported as a failure. The deal
        may well be open; saying otherwise invites a duplicate.
        """
        try:
            confirm = self._request("GET", f"/confirms/{deal_ref}", version="1")
        except BrokerError as exc:
            log.error(
                "broker.ig_confirm_unreadable",
                symbol=signal.symbol, deal_reference=deal_ref, error=str(exc),
            )
            return OrderResult(
                accepted=True,
                broker=self.name,
                symbol=signal.symbol,
                qty=sizing.shares,
                side=side.lower(),
                broker_order_id=deal_ref,
                status="unconfirmed",
                message=(
                    f"Deal {deal_ref} was sent but its confirmation could not be "
                    f"read ({exc}). CHECK YOUR IG ACCOUNT before placing anything "
                    "else in this market — it may be open."
                ),
            )

        status = str(confirm.get("dealStatus", "")).upper()
        reason = str(confirm.get("reason", ""))
        if status != "ACCEPTED":
            log.error(
                "broker.ig_deal_not_accepted",
                symbol=signal.symbol, epic=epic, status=status, reason=reason,
            )
            return OrderResult(
                accepted=False,
                broker=self.name,
                symbol=signal.symbol,
                qty=sizing.shares,
                side=side.lower(),
                broker_order_id=str(confirm.get("dealId") or deal_ref),
                status=status.lower() or "rejected",
                message=_reject_reason(reason),
                raw=confirm,
            )

        filled = float(confirm.get("level") or signal.entry)
        slip = filled - signal.entry
        log.info(
            "broker.ig_deal_accepted",
            symbol=signal.symbol,
            epic=epic,
            side=side,
            size=sizing.shares,
            unit=sizing.unit,
            level=filled,
            slippage=round(slip, 4),
            deal_id=confirm.get("dealId"),
            demo=self.is_paper,
        )
        if signal.risk_per_share > 0 and abs(slip) > signal.risk_per_share * 0.25:
            log.warning(
                "broker.ig_significant_slippage",
                symbol=signal.symbol,
                expected=signal.entry,
                filled=filled,
                note="filled far from the signal price; the planned R:R no longer holds",
            )

        return OrderResult(
            accepted=True,
            broker=self.name,
            symbol=signal.symbol,
            qty=sizing.shares,
            side=side.lower(),
            broker_order_id=str(confirm.get("dealId") or deal_ref),
            status="filled",
            message=f"filled at {filled:,.2f}",
            raw=confirm,
        )

    def close_position(self, symbol: str) -> OrderResult:
        """
        Close by dealId, in the opposite direction, at market.

        IG uses a POST with an `_method: DELETE` header for this — not a real
        DELETE. Sending an actual DELETE returns 405.
        """
        try:
            epic = self._epics.get(symbol.upper(), symbol)
            positions = self._request("GET", "/positions", version="2").get("positions") or []
        except BrokerError as exc:
            return OrderResult(
                accepted=False, broker=self.name, symbol=symbol,
                qty=0, side="", message=str(exc),
            )

        match = next(
            (
                row
                for row in positions
                if str((row.get("market") or {}).get("epic") or "") in (epic, symbol.upper())
            ),
            None,
        )
        if match is None:
            return OrderResult(
                accepted=False, broker=self.name, symbol=symbol,
                qty=0, side="", message=f"no open IG position in {symbol}",
            )

        pos = match.get("position") or {}
        size = float(pos.get("dealSize") or pos.get("size") or 0.0)
        opposite = "SELL" if str(pos.get("direction", "BUY")).upper() == "BUY" else "BUY"

        try:
            data = self._request(
                "POST",
                "/positions/otc",
                version="1",
                extra_headers={"_method": "DELETE"},
                json={
                    "dealId": str(pos.get("dealId") or ""),
                    "direction": opposite,
                    "size": round(size, 2),
                    "orderType": "MARKET",
                },
            )
        except BrokerError as exc:
            return OrderResult(
                accepted=False, broker=self.name, symbol=symbol,
                qty=size, side="close", message=str(exc),
            )

        deal_ref = str(data.get("dealReference") or "")
        log.info("broker.ig_close_sent", symbol=symbol, size=size, deal_reference=deal_ref)
        return OrderResult(
            accepted=True,
            broker=self.name,
            symbol=symbol,
            qty=size,
            side="close",
            broker_order_id=deal_ref,
            status="closing",
            raw=data,
        )


# ---------------------------------------------------------------------- #
def _explain(resp: httpx.Response) -> str:
    """Turn an IG error body into something a human can act on."""
    code = ""
    try:
        body = resp.json()
        if isinstance(body, dict):
            code = str(body.get("errorCode") or "")
    except ValueError:
        body = None
    hint = ERROR_HINTS.get(code)
    if hint:
        return f"{code} — {hint}"
    if code:
        return code
    return resp.text[:300] or f"HTTP {resp.status_code} with no body"


def _reject_reason(reason: str) -> str:
    """IG's deal rejection reasons, in English."""
    known = {
        "INSUFFICIENT_BALANCE": (
            "IG refused the deal for insufficient margin. Lower "
            "RISK_PER_TRADE_PCT or IG_MAX_EXPOSURE_PCT, or fund the account."
        ),
        "MARKET_CLOSED": "that IG market is closed right now.",
        "MARKET_CLOSED_WITH_EDITS": "that IG market is closed right now.",
        "MARKET_OFFLINE": "that IG market is offline.",
        "ATTACHED_ORDER_LEVEL_ERROR": (
            "IG rejected the stop or limit level — usually because it is closer "
            "to the current price than that market's minimum stop distance."
        ),
        "ATTACHED_ORDER_TRAILING_STOP_ERROR": "IG rejected the trailing stop setup.",
        "MINIMUM_ORDER_SIZE_ERROR": (
            "the stake is below IG's minimum for that market. Raise "
            "IG_MIN_DEAL_SIZE to match, or widen the account."
        ),
        "SIZE_INCREMENT_ERROR": "the stake is not a size increment IG accepts for that market.",
        "INSTRUMENT_NOT_TRADEABLE_IN_THIS_CURRENCY": "wrong currency for that market.",
        "OPPOSING_POSITIONS_NOT_ALLOWED": (
            "IG will not hold opposing positions in that market on this account."
        ),
        "POSITION_NOT_AVAILABLE_TO_CLOSE": "that position is no longer open at IG.",
        "REJECT_CFD_ORDER_ON_SPREADBET_ACCOUNT": (
            "a CFD order was sent to a spread betting account."
        ),
        "REJECT_SPREADBET_ORDER_ON_CFD_ACCOUNT": (
            "a spread bet was sent to a CFD account."
        ),
        "MANUAL_ORDER_TIMEOUT": "IG's manual dealing desk did not respond in time.",
        "CURRENCY_NOT_ALLOWED": "that currency is not allowed on this account.",
    }
    text = known.get(reason.upper())
    return f"IG rejected the deal: {text}" if text else f"IG rejected the deal ({reason})."
