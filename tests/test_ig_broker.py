"""
The IG adapter, against a fake IG.

These are mocked at the HTTP layer rather than at the method layer on purpose:
the things most likely to be wrong about this adapter are the wire details —
which VERSION header, the `_method: DELETE` close, `forceOpen`, and the
two-step deal/confirm dance — and a mock of `_request` would assert none of
them. No test here reaches the real IG API; the adapter has never run against
a funded account.
"""
from __future__ import annotations

import json

import httpx
import pytest

from src.broker.base import BrokerError
from src.broker.ig import IGBroker
from src.config import LIVE_CONFIRMATION_PHRASE, Settings
from src.models import PositionSizing, Signal


def make_settings(**kw) -> Settings:
    kw.setdefault("anthropic_api_key", "test")
    kw.setdefault("ig_api_key", "igkey123")
    kw.setdefault("ig_username", "tester")
    kw.setdefault("ig_password", "hunter2")
    kw.setdefault("ig_min_request_interval", 0.0)  # don't sleep in tests
    return Settings(_env_file=None, **kw)


LOGIN_BODY = {
    "currentAccountId": "ABC12",
    "accounts": [
        {"accountId": "ABC12", "accountType": "SPREADBET", "currency": "GBP"},
        {"accountId": "XYZ99", "accountType": "CFD", "currency": "USD"},
    ],
}


def path_of(request: httpx.Request) -> str:
    """IG's base URL carries a /gateway/deal prefix that routes do not."""
    return request.url.path.removeprefix("/gateway/deal")


class FakeIG:
    """Records every request and replies from a canned route table."""

    def __init__(self, routes: dict[str, object] | None = None) -> None:
        self.routes: dict[str, object] = routes or {}
        self.calls: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        key = f"{request.method} {path_of(request)}"

        if key == "POST /session":
            return httpx.Response(
                200,
                json=LOGIN_BODY,
                headers={"CST": "cst-token", "X-SECURITY-TOKEN": "xst-token"},
            )

        route = self.routes.get(key)
        if route is None:
            return httpx.Response(404, json={"errorCode": "error.route.not-stubbed"})
        if callable(route):
            return route(request)
        return httpx.Response(200, json=route)

    # -- helpers --------------------------------------------------------- #
    def body(self, index: int) -> dict:
        return json.loads(self.calls[index].content or b"{}")

    def paths(self) -> list[str]:
        return [f"{c.method} {path_of(c)}" for c in self.calls]


def build(fake: FakeIG, settings: Settings | None = None) -> IGBroker:
    settings = settings or make_settings()
    broker = IGBroker(settings)
    broker._client.close()
    broker._client = httpx.Client(
        base_url=settings.ig_base_url,
        transport=httpx.MockTransport(fake.handler),
        headers={"X-IG-API-KEY": settings.ig_api_key},
    )
    return broker


def a_signal(**kw) -> Signal:
    defaults = dict(
        symbol="FTSE",
        direction="long",
        entry=8000.0,
        stop=7960.0,
        target=8100.0,
        confidence=0.8,
        reasoning="test",
    )
    defaults.update(kw)
    return Signal(**defaults)  # type: ignore[arg-type]


def a_sizing(stake: float = 2.5) -> PositionSizing:
    return PositionSizing(
        shares=stake,
        dollar_risk=stake * 40,
        risk_per_share=40.0,
        notional=stake * 8000,
        account_risk_pct=1.0,
        unit="GBP/point",
    )


# ---------------------------------------------------------------------- #
# Session
# ---------------------------------------------------------------------- #
def test_missing_credentials_is_refused_before_any_request():
    with pytest.raises(BrokerError, match="IG_API_KEY"):
        IGBroker(Settings(_env_file=None, anthropic_api_key="t"))


def test_demo_host_by_default():
    assert "demo-api.ig.com" in make_settings().ig_base_url
    assert "demo-api" not in make_settings(ig_demo=False).ig_base_url


def test_login_stores_the_token_pair_and_account():
    fake = FakeIG()
    broker = build(fake)
    broker.login()

    assert broker.authenticated
    assert broker._cst == "cst-token"
    assert broker._security_token == "xst-token"
    assert broker._account_id == "ABC12"
    assert broker._currency == "GBP"
    # VERSION 2 is required on /session; VERSION 1 returns a different shape.
    assert fake.calls[0].headers["VERSION"] == "2"
    assert fake.body(0) == {"identifier": "tester", "password": "hunter2"}


def test_login_honours_a_configured_account_id():
    broker = build(FakeIG(), make_settings(ig_account_id="XYZ99"))
    broker.login()
    assert broker._account_id == "XYZ99"
    assert broker._currency == "USD"


def test_login_falls_back_when_the_configured_account_does_not_exist():
    """A typo'd account id must not silently deal on whatever IG defaults to
    without saying so — but it must also not take the whole bot down."""
    broker = build(FakeIG(), make_settings(ig_account_id="NOPE1"))
    broker.login()
    assert broker._account_id == "ABC12"


def test_a_key_from_the_wrong_environment_is_explained():
    def session(request):
        return httpx.Response(403, json={"errorCode": "error.security.api-key-invalid"})

    broker = build(FakeIG())
    broker._client = httpx.Client(
        base_url="https://demo-api.ig.com/gateway/deal",
        transport=httpx.MockTransport(session),
    )
    with pytest.raises(BrokerError, match="Demo and live keys are NOT interchangeable"):
        broker.login()


def test_encryption_requirement_is_explained():
    def session(request):
        return httpx.Response(
            403, json={"errorCode": "error.public-api.failure.encryption.required"}
        )

    broker = build(FakeIG())
    broker._client = httpx.Client(
        base_url="https://demo-api.ig.com/gateway/deal",
        transport=httpx.MockTransport(session),
    )
    with pytest.raises(BrokerError, match="Encrypted passwords"):
        broker.login()


def test_is_paper_follows_ig_demo_not_the_url():
    assert build(FakeIG(), make_settings(ig_demo=True)).is_paper
    assert not build(FakeIG(), make_settings(ig_demo=False)).is_paper


# ---------------------------------------------------------------------- #
# Account and positions
# ---------------------------------------------------------------------- #
def test_equity_includes_open_position_pnl():
    """
    IG reports closed-trade balance and open P&L separately. If equity ignored
    the open mark, a book down 4% on the day would never trip the daily stop.
    """
    fake = FakeIG(
        {
            "GET /accounts": {
                "accounts": [
                    {
                        "accountId": "ABC12",
                        "currency": "GBP",
                        "balance": {
                            "balance": 10_000.0,
                            "profitLoss": -400.0,
                            "available": 7_500.0,
                            "deposit": 2_100.0,
                        },
                    }
                ]
            }
        }
    )
    broker = build(fake)
    account = broker.get_account()

    assert account.equity == pytest.approx(9_600.0)
    assert account.buying_power == pytest.approx(7_500.0)
    assert account.day_pnl == pytest.approx(-400.0)
    assert account.day_pnl_pct == pytest.approx(-4.0)
    assert "GET /accounts" in fake.paths()


def test_no_accounts_is_an_error_not_an_empty_account():
    broker = build(FakeIG({"GET /accounts": {"accounts": []}}))
    with pytest.raises(BrokerError, match="no accounts"):
        broker.get_account()


def test_positions_are_marked_on_the_side_you_would_exit_at():
    fake = FakeIG(
        {
            "GET /positions": {
                "positions": [
                    {
                        "position": {
                            "dealId": "DEAL1",
                            "dealSize": 2.0,
                            "direction": "BUY",
                            "level": 8000.0,
                        },
                        "market": {"epic": "IX.D.FTSE.DAILY.IP", "bid": 8050.0, "offer": 8052.0},
                    },
                    {
                        "position": {
                            "dealId": "DEAL2",
                            "dealSize": 1.5,
                            "direction": "SELL",
                            "level": 100.0,
                        },
                        "market": {"epic": "UA.D.AAPL.CASH.IP", "bid": 95.0, "offer": 96.0},
                    },
                ]
            }
        }
    )
    long_pos, short_pos = build(fake).get_positions()

    # A long exits at the bid, so it is marked there, not at the offer.
    assert long_pos.qty == 2.0
    assert long_pos.market_price == 8050.0
    assert long_pos.unrealized_pnl == pytest.approx(100.0)

    # A short is negative-signed and marked at the offer.
    assert short_pos.qty == -1.5
    assert short_pos.side == "short"
    assert short_pos.market_price == 96.0
    assert short_pos.unrealized_pnl == pytest.approx(6.0)


def test_positions_uses_version_2():
    fake = FakeIG({"GET /positions": {"positions": []}})
    build(fake).get_positions()
    call = next(c for c in fake.calls if path_of(c) == "/positions")
    assert call.headers["VERSION"] == "2"


def test_ig_does_not_claim_to_support_crypto():
    broker = build(FakeIG())
    assert broker.supports("equity")
    assert not broker.supports("crypto")


# ---------------------------------------------------------------------- #
# Epic resolution
# ---------------------------------------------------------------------- #
def test_pinned_epics_skip_the_search_entirely():
    fake = FakeIG()
    broker = build(fake, make_settings(ig_epics="AAPL=UA.D.AAPL.CASH.IP"))
    assert broker.resolve_epic("aapl") == "UA.D.AAPL.CASH.IP"
    assert "GET /markets" not in fake.paths()


def test_search_prefers_a_non_expiring_market():
    """A dated future is a different instrument at a different price."""
    fake = FakeIG(
        {
            "GET /markets": {
                "markets": [
                    {"epic": "IX.D.FTSE.MONTH1.IP", "expiry": "DEC-25", "instrumentName": "FTSE Dec"},
                    {"epic": "IX.D.FTSE.DAILY.IP", "expiry": "-", "instrumentName": "FTSE Cash"},
                ]
            }
        }
    )
    broker = build(fake)
    assert broker.resolve_epic("FTSE") == "IX.D.FTSE.DAILY.IP"


def test_a_resolved_epic_is_cached():
    fake = FakeIG({"GET /markets": {"markets": [{"epic": "E1", "expiry": "-"}]}})
    broker = build(fake)
    broker.resolve_epic("FTSE")
    broker.resolve_epic("FTSE")
    assert fake.paths().count("GET /markets") == 1


def test_an_unmatched_symbol_says_how_to_fix_it():
    broker = build(FakeIG({"GET /markets": {"markets": []}}))
    with pytest.raises(BrokerError, match="IG_EPICS"):
        broker.resolve_epic("NOSUCH")


# ---------------------------------------------------------------------- #
# Dealing
# ---------------------------------------------------------------------- #
def _deal_routes(confirm: object) -> dict[str, object]:
    return {
        "POST /positions/otc": {"dealReference": "REF123"},
        "GET /confirms/REF123": confirm,
    }


def test_a_deal_carries_its_stop_and_limit_and_forces_open():
    """
    The whole point of using IG's attached levels: there is never a moment
    where the position exists without a stop. forceOpen is load-bearing —
    without it IG may net the deal and discard the levels.
    """
    fake = FakeIG(
        _deal_routes({"dealStatus": "ACCEPTED", "dealId": "D1", "level": 8001.0})
    )
    broker = build(fake, make_settings(ig_epics="FTSE=IX.D.FTSE.DAILY.IP"))
    result = broker.submit_bracket_order(a_signal(), a_sizing(2.5))

    assert result.accepted
    assert result.status == "filled"
    assert result.broker_order_id == "D1"

    deal = next(c for c in fake.calls if path_of(c) == "/positions/otc")
    body = json.loads(deal.content)
    assert deal.headers["VERSION"] == "2"
    assert body["epic"] == "IX.D.FTSE.DAILY.IP"
    assert body["direction"] == "BUY"
    assert body["size"] == 2.5
    assert body["orderType"] == "MARKET"
    assert body["stopLevel"] == 7960.0
    assert body["limitLevel"] == 8100.0
    assert body["forceOpen"] is True
    assert body["guaranteedStop"] is False
    assert body["expiry"] == "-"


def test_a_short_sells():
    fake = FakeIG(_deal_routes({"dealStatus": "ACCEPTED", "dealId": "D2", "level": 99.0}))
    broker = build(fake, make_settings(ig_epics="AAPL=UA.D.AAPL.CASH.IP"))
    signal = a_signal(symbol="AAPL", direction="short", entry=100, stop=104, target=92)
    result = broker.submit_bracket_order(signal, a_sizing(1.0))

    assert result.accepted
    body = json.loads(next(c for c in fake.calls if path_of(c) == "/positions/otc").content)
    assert body["direction"] == "SELL"
    assert body["stopLevel"] == 104.0
    assert body["limitLevel"] == 92.0


def test_zero_size_never_reaches_ig():
    fake = FakeIG()
    result = build(fake).submit_bracket_order(a_signal(), a_sizing(0.0))
    assert not result.accepted
    assert "POST /positions/otc" not in fake.paths()


def test_a_rejected_deal_is_reported_with_a_readable_reason():
    fake = FakeIG(
        _deal_routes({"dealStatus": "REJECTED", "reason": "INSUFFICIENT_BALANCE"})
    )
    broker = build(fake, make_settings(ig_epics="FTSE=IX.D.FTSE.DAILY.IP"))
    result = broker.submit_bracket_order(a_signal(), a_sizing())

    assert not result.accepted
    assert "insufficient margin" in result.message
    assert result.status == "rejected"


def test_a_stop_too_close_to_price_is_explained():
    fake = FakeIG(
        _deal_routes({"dealStatus": "REJECTED", "reason": "ATTACHED_ORDER_LEVEL_ERROR"})
    )
    broker = build(fake, make_settings(ig_epics="FTSE=IX.D.FTSE.DAILY.IP"))
    result = broker.submit_bracket_order(a_signal(), a_sizing())
    assert not result.accepted
    assert "minimum stop distance" in result.message


def test_an_unreadable_confirmation_is_not_reported_as_a_failure():
    """
    The most dangerous case in the whole adapter. IG took the deal and then the
    confirm call failed. Reporting "rejected" invites a human or an auto-retry
    to send it again, and now there are two geared positions. It must come back
    as accepted-but-unconfirmed with a loud instruction to go and look.
    """
    def confirm(request):
        return httpx.Response(503, json={"errorCode": "error.service.unavailable"})

    fake = FakeIG(
        {"POST /positions/otc": {"dealReference": "REF123"}, "GET /confirms/REF123": confirm}
    )
    broker = build(fake, make_settings(ig_epics="FTSE=IX.D.FTSE.DAILY.IP"))
    result = broker.submit_bracket_order(a_signal(), a_sizing())

    assert result.accepted is True
    assert result.status == "unconfirmed"
    assert result.broker_order_id == "REF123"
    assert "CHECK YOUR IG ACCOUNT" in result.message


def test_a_missing_deal_reference_is_a_failure():
    fake = FakeIG({"POST /positions/otc": {}})
    broker = build(fake, make_settings(ig_epics="FTSE=IX.D.FTSE.DAILY.IP"))
    result = broker.submit_bracket_order(a_signal(), a_sizing())
    assert not result.accepted
    assert "no dealReference" in result.message


def test_an_order_is_never_resubmitted_after_an_auth_failure():
    """
    Reads re-authenticate and retry. Writes must not: a 401 on a POST may
    arrive after IG has already taken the deal.
    """
    attempts = {"n": 0}

    def deal(request):
        attempts["n"] += 1
        return httpx.Response(401, json={"errorCode": "error.security.account-token-invalid"})

    fake = FakeIG({"POST /positions/otc": deal})
    broker = build(fake, make_settings(ig_epics="FTSE=IX.D.FTSE.DAILY.IP"))
    result = broker.submit_bracket_order(a_signal(), a_sizing())

    assert not result.accepted
    assert attempts["n"] == 1


def test_a_read_does_reauthenticate_once():
    state = {"calls": 0}

    def positions(request):
        state["calls"] += 1
        if state["calls"] == 1:
            return httpx.Response(401, json={"errorCode": "error.security.account-token-invalid"})
        return httpx.Response(200, json={"positions": []})

    fake = FakeIG({"GET /positions": positions})
    broker = build(fake)
    assert broker.get_positions() == []
    assert state["calls"] == 2
    # Two logins: the initial one, and one after the token was rejected.
    assert fake.paths().count("POST /session") == 2


def test_significant_slippage_is_surfaced_in_the_fill_price():
    fake = FakeIG(
        _deal_routes({"dealStatus": "ACCEPTED", "dealId": "D9", "level": 8030.0})
    )
    broker = build(fake, make_settings(ig_epics="FTSE=IX.D.FTSE.DAILY.IP"))
    result = broker.submit_bracket_order(a_signal(), a_sizing())
    assert result.accepted
    assert "8,030" in result.message


# ---------------------------------------------------------------------- #
# Closing
# ---------------------------------------------------------------------- #
OPEN_POSITION = {
    "positions": [
        {
            "position": {"dealId": "DEAL1", "dealSize": 2.0, "direction": "BUY", "level": 8000.0},
            "market": {"epic": "IX.D.FTSE.DAILY.IP", "bid": 8050.0, "offer": 8052.0},
        }
    ]
}


def test_closing_posts_with_the_method_delete_header():
    """IG closes a position with POST + `_method: DELETE`. A real DELETE is a 405."""
    fake = FakeIG(
        {"GET /positions": OPEN_POSITION, "POST /positions/otc": {"dealReference": "CLOSE1"}}
    )
    broker = build(fake, make_settings(ig_epics="FTSE=IX.D.FTSE.DAILY.IP"))
    result = broker.close_position("FTSE")

    assert result.accepted
    assert result.broker_order_id == "CLOSE1"

    close = next(c for c in fake.calls if c.method == "POST" and path_of(c) == "/positions/otc")
    assert close.headers["_method"] == "DELETE"
    body = json.loads(close.content)
    assert body["dealId"] == "DEAL1"
    assert body["direction"] == "SELL"  # opposite of the open BUY
    assert body["size"] == 2.0
    assert body["orderType"] == "MARKET"


def test_closing_a_short_buys_back():
    short = {
        "positions": [
            {
                "position": {"dealId": "D2", "dealSize": 1.0, "direction": "SELL", "level": 100.0},
                "market": {"epic": "UA.D.AAPL.CASH.IP", "bid": 95.0, "offer": 96.0},
            }
        ]
    }
    fake = FakeIG(
        {"GET /positions": short, "POST /positions/otc": {"dealReference": "C2"}}
    )
    broker = build(fake, make_settings(ig_epics="AAPL=UA.D.AAPL.CASH.IP"))
    broker.close_position("AAPL")
    close = next(c for c in fake.calls if c.method == "POST" and path_of(c) == "/positions/otc")
    assert json.loads(close.content)["direction"] == "BUY"


def test_closing_something_not_held_is_refused_without_a_deal():
    fake = FakeIG({"GET /positions": {"positions": []}})
    result = build(fake).close_position("FTSE")
    assert not result.accepted
    assert "no open IG position" in result.message
    assert "POST /positions/otc" not in fake.paths()


def test_position_deal_ids_maps_epics():
    fake = FakeIG({"GET /positions": OPEN_POSITION})
    assert build(fake).position_deal_ids() == {"IX.D.FTSE.DAILY.IP": "DEAL1"}


# ---------------------------------------------------------------------- #
# Market state
# ---------------------------------------------------------------------- #
def test_dealing_rules_are_read_from_the_market():
    fake = FakeIG(
        {
            "GET /markets/IX.D.FTSE.DAILY.IP": {
                "dealingRules": {
                    "minDealSize": {"unit": "AMOUNT", "value": 0.5},
                    "minNormalStopOrLimitDistance": {"unit": "POINTS", "value": 8.0},
                },
                "snapshot": {"marketStatus": "TRADEABLE", "scalingFactor": 1},
            }
        }
    )
    broker = build(fake, make_settings(ig_epics="FTSE=IX.D.FTSE.DAILY.IP"))
    rules = broker.dealing_rules("IX.D.FTSE.DAILY.IP")

    assert rules["min_deal_size"] == 0.5
    assert rules["min_stop_distance"] == 8.0
    assert rules["market_status"] == "TRADEABLE"
    call = next(c for c in fake.calls if path_of(c).startswith("/markets/"))
    assert call.headers["VERSION"] == "3"


def test_market_closed_is_detected():
    fake = FakeIG(
        {
            "GET /markets/IX.D.FTSE.DAILY.IP": {
                "snapshot": {"marketStatus": "EDITS_ONLY"},
            }
        }
    )
    broker = build(fake, make_settings(ig_epics="FTSE=IX.D.FTSE.DAILY.IP"))
    assert broker.is_market_open() is False


def test_market_status_defaults_open_when_it_cannot_be_read():
    """A false 'closed' silently stops the bot for a day; a false 'open' costs
    one wasted API call, because IG refuses deals on closed markets anyway."""
    fake = FakeIG({})  # /markets/{epic} is not stubbed -> 404
    broker = build(fake, make_settings(ig_epics="FTSE=IX.D.FTSE.DAILY.IP"))
    assert broker.is_market_open() is True


def test_no_epics_configured_means_nothing_to_check():
    assert build(FakeIG()).is_market_open() is True


# ---------------------------------------------------------------------- #
# Rate limiting
# ---------------------------------------------------------------------- #
def test_a_429_names_the_settings_that_fix_it():
    def positions(request):
        return httpx.Response(429, json={"errorCode": "error.public-api.exceeded-account-allowance"})

    broker = build(FakeIG({"GET /positions": positions}))
    with pytest.raises(BrokerError, match="SCAN_INTERVAL_MINUTES"):
        broker.get_positions()


def test_the_throttle_waits_between_calls(monkeypatch):
    slept: list[float] = []
    monkeypatch.setattr("src.broker.ig.time.sleep", lambda s: slept.append(s))

    fake = FakeIG({"GET /positions": {"positions": []}})
    broker = build(fake, make_settings(ig_min_request_interval=0.5))
    broker.get_positions()
    broker.get_positions()

    assert slept, "expected the adapter to space its requests"
    assert all(0 < s <= 0.5 for s in slept)


# ---------------------------------------------------------------------- #
# The live gate
# ---------------------------------------------------------------------- #
def test_ig_live_needs_its_own_switch_not_alpacas():
    """
    A leftover ALPACA_PAPER=false must not authorise live spread bets.
    """
    s = make_settings(
        trading_mode="live", alpaca_paper=False, confirm_live=LIVE_CONFIRMATION_PHRASE
    )
    assert s.active_broker == "ig"
    assert not s.live_trading_enabled
    assert "IG_DEMO is true" in s.live_mode_blockers()

    live = make_settings(
        trading_mode="live",
        ig_demo=False,
        alpaca_paper=True,
        confirm_live=LIVE_CONFIRMATION_PHRASE,
    )
    assert live.live_trading_enabled
    assert live.live_mode_blockers() == []
