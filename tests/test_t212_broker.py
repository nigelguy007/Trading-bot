"""
The Trading 212 adapter, against a fake T212.

Two things get the most attention here, because they are what can cost money:

  * the entry/stop/target SEQUENCE. T212 has no bracket order, so the adapter
    buys, waits for the fill, then places a stop and a target as separate
    orders. The stop must go first, a failed stop must be shouted about, and
    a sell must be a NEGATIVE quantity — T212 has no `side` field, so a sign
    error is a buy where a sell was meant.
  * `reconcile()`, which is the OCO that T212 does not provide. Without it a
    filled target leaves its stop resting against shares that are gone.

`test_t212_spec.py` separately checks these request bodies against T212's own
OpenAPI document, so a schema change upstream fails a test rather than an
order.
"""
from __future__ import annotations

import json

import httpx
import pytest

from src.broker.base import BrokerError
from src.broker.t212 import Trading212Broker
from src.config import Settings
from src.models import PositionSizing, Signal


@pytest.fixture(autouse=True)
def no_sleeping(monkeypatch):
    """The adapter's rate-limit floor is real seconds. Not in tests."""
    monkeypatch.setattr("src.broker.t212.time.sleep", lambda _s: None)


def make_settings(**kw) -> Settings:
    kw.setdefault("anthropic_api_key", "test")
    kw.setdefault("t212_api_key", "t212key")
    kw.setdefault("t212_fill_timeout", 5.0)
    return Settings(_env_file=None, **kw)


class FakeT212:
    def __init__(self, routes: dict[str, object] | None = None) -> None:
        self.routes: dict[str, object] = routes or {}
        self.calls: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        key = f"{request.method} {request.url.path}"
        route = self.routes.get(key)
        if route is None:
            return httpx.Response(404, json={"error": f"not stubbed: {key}"})
        if callable(route):
            return route(request)
        return httpx.Response(200, json=route)

    def paths(self) -> list[str]:
        return [f"{c.method} {c.url.path}" for c in self.calls]

    def bodies(self, path: str) -> list[dict]:
        return [
            json.loads(c.content)
            for c in self.calls
            if c.url.path == path and c.content
        ]


def build(fake: FakeT212, settings: Settings | None = None) -> Trading212Broker:
    settings = settings or make_settings()
    broker = Trading212Broker(settings)
    broker._client.close()
    broker._client = httpx.Client(
        base_url=settings.t212_base_url,
        transport=httpx.MockTransport(fake.handler),
        headers=Trading212Broker._auth_headers(settings),
    )
    return broker


def a_signal(**kw) -> Signal:
    defaults = dict(
        symbol="AAPL", direction="long", entry=100.0, stop=96.0,
        target=112.0, confidence=0.8, reasoning="test",
    )
    defaults.update(kw)
    return Signal(**defaults)  # type: ignore[arg-type]


def a_sizing(shares: float = 25.0) -> PositionSizing:
    return PositionSizing(
        shares=shares, dollar_risk=shares * 4, risk_per_share=4.0,
        notional=shares * 100, account_risk_pct=1.0, unit="shares",
    )


ORDERS = "/api/v0/equity/orders"
MARKET = "/api/v0/equity/orders/market"
STOP = "/api/v0/equity/orders/stop"
LIMIT = "/api/v0/equity/orders/limit"
POSITIONS = "/api/v0/equity/positions"
SUMMARY = "/api/v0/equity/account/summary"


# ---------------------------------------------------------------------- #
# Setup and auth
# ---------------------------------------------------------------------- #
def test_missing_key_is_refused_before_any_request():
    with pytest.raises(BrokerError, match="T212_API_KEY"):
        Trading212Broker(Settings(_env_file=None, anthropic_api_key="t"))


def test_demo_host_by_default():
    assert make_settings().t212_base_url == "https://demo.trading212.com"
    assert make_settings(t212_demo=False).t212_base_url == "https://live.trading212.com"


def test_basic_auth_is_used_when_a_secret_is_set():
    headers = Trading212Broker._auth_headers(
        make_settings(t212_api_key="key", t212_api_secret="secret")
    )
    # base64("key:secret")
    assert headers["Authorization"] == "Basic a2V5OnNlY3JldA=="


def test_the_legacy_raw_key_header_is_used_when_there_is_no_secret():
    headers = Trading212Broker._auth_headers(make_settings(t212_api_key="rawkey"))
    assert headers["Authorization"] == "rawkey"


def test_is_paper_follows_t212_demo():
    assert build(FakeT212(), make_settings()).is_paper
    assert not build(FakeT212(), make_settings(t212_demo=False)).is_paper


def test_t212_does_not_claim_crypto():
    broker = build(FakeT212())
    assert broker.supports("equity")
    assert not broker.supports("crypto")


def test_a_403_explains_the_account_type_requirement():
    fake = FakeT212({f"GET {POSITIONS}": lambda r: httpx.Response(403, json={})})
    with pytest.raises(BrokerError, match="Invest and Stocks ISA"):
        build(fake).get_positions()


# ---------------------------------------------------------------------- #
# Account and positions
# ---------------------------------------------------------------------- #
def test_equity_sums_cash_and_holdings():
    fake = FakeT212(
        {
            f"GET {SUMMARY}": {
                "currency": "GBP",
                "cash": {"availableToTrade": 2_000.0, "reservedForOrders": 500.0, "inPies": 250.0},
                "investments": {"currentValue": 7_250.0, "unrealizedProfitLoss": 300.0},
            }
        }
    )
    account = build(fake).get_account()

    assert account.equity == pytest.approx(10_000.0)
    assert account.cash == pytest.approx(2_000.0)
    assert account.buying_power == pytest.approx(2_000.0)


def test_no_start_of_day_equity_is_reported_so_the_journal_supplies_it():
    """
    T212 has no such field. Reporting a made-up one would give the daily loss
    limit a baseline it could not justify; 0 tells the portfolio layer to use
    the journal instead.
    """
    fake = FakeT212({f"GET {SUMMARY}": {"cash": {"availableToTrade": 100.0}, "investments": {}}})
    assert build(fake).get_account().last_equity == 0.0


def test_positions_are_long_and_venue_tagged():
    fake = FakeT212(
        {
            f"GET {POSITIONS}": [
                {
                    "instrument": {"ticker": "AAPL_US_EQ", "currency": "USD"},
                    "quantity": 12.0,
                    "averagePricePaid": 100.0,
                    "currentPrice": 110.0,
                    "walletImpact": {"unrealizedProfitLoss": 95.0, "currency": "GBP"},
                }
            ]
        }
    )
    (position,) = build(fake).get_positions()

    assert position.symbol == "AAPL"
    assert position.qty == 12.0
    assert position.side == "long"
    assert position.venue == "t212"
    # The broker's own figure wins: it is in ACCOUNT currency, whereas
    # recomputing from the prices would be in INSTRUMENT currency.
    assert position.unrealized_pnl == pytest.approx(95.0)


def test_pnl_is_derived_when_the_broker_omits_it():
    fake = FakeT212(
        {
            f"GET {POSITIONS}": [
                {
                    "instrument": {"ticker": "MSFT_US_EQ"},
                    "quantity": 10.0,
                    "averagePricePaid": 50.0,
                    "currentPrice": 55.0,
                    "walletImpact": {},
                }
            ]
        }
    )
    (position,) = build(fake).get_positions()
    assert position.unrealized_pnl == pytest.approx(50.0)


def test_zero_quantity_rows_are_skipped():
    fake = FakeT212(
        {f"GET {POSITIONS}": [{"instrument": {"ticker": "X_US_EQ"}, "quantity": 0.0}]}
    )
    assert build(fake).get_positions() == []


# ---------------------------------------------------------------------- #
# Ticker resolution
# ---------------------------------------------------------------------- #
def test_pinned_tickers_skip_the_instruments_download():
    fake = FakeT212()
    broker = build(fake, make_settings(t212_tickers="AAPL=AAPL_US_EQ"))
    assert broker.resolve_ticker("aapl") == "AAPL_US_EQ"
    assert fake.calls == []


def test_tickers_are_resolved_from_the_instrument_list_and_cached():
    """That endpoint allows one request per 50 seconds, so it must load once."""
    fake = FakeT212(
        {
            "GET /api/v0/equity/metadata/instruments": [
                {"ticker": "AAPL_US_EQ", "shortName": "AAPL"},
                {"ticker": "VODl_EQ", "shortName": "VOD"},
            ]
        }
    )
    broker = build(fake)
    assert broker.resolve_ticker("VOD") == "VODl_EQ"
    assert broker.resolve_ticker("AAPL") == "AAPL_US_EQ"
    assert fake.paths().count("GET /api/v0/equity/metadata/instruments") == 1


def test_an_unknown_symbol_says_how_to_pin_it():
    fake = FakeT212({"GET /api/v0/equity/metadata/instruments": []})
    with pytest.raises(BrokerError, match="T212_TICKERS"):
        build(fake).resolve_ticker("NOSUCH")


def test_an_explicit_t212_ticker_passes_through():
    assert build(FakeT212()).resolve_ticker("AAPL_US_EQ") == "AAPL_US_EQ"


# ---------------------------------------------------------------------- #
# The entry / stop / target sequence
# ---------------------------------------------------------------------- #
def _filled(order_id: int = 1, qty: float = 25.0):
    return {
        f"POST {MARKET}": {"id": order_id, "status": "NEW"},
        f"GET {ORDERS}/{order_id}": {
            "id": order_id, "status": "FILLED", "filledQuantity": qty
        },
        f"POST {STOP}": {"id": 2, "status": "NEW"},
        f"POST {LIMIT}": {"id": 3, "status": "NEW"},
    }


def test_the_full_sequence_buys_then_protects_in_order():
    fake = FakeT212(_filled())
    broker = build(fake, make_settings(t212_tickers="AAPL=AAPL_US_EQ"))
    result = broker.submit_bracket_order(a_signal(), a_sizing(25))

    assert result.accepted
    assert result.status == "filled"
    assert result.broker_order_id == "1"

    posts = [p for p in fake.paths() if p.startswith("POST")]
    # The stop must be placed before the target: if only one can be placed it
    # has to be the one that caps the loss.
    assert posts == [f"POST {MARKET}", f"POST {STOP}", f"POST {LIMIT}"]


def test_the_entry_is_a_positive_quantity_and_the_exits_are_negative():
    """T212 has no `side` field — the sign IS the side."""
    fake = FakeT212(_filled())
    broker = build(fake, make_settings(t212_tickers="AAPL=AAPL_US_EQ"))
    broker.submit_bracket_order(a_signal(), a_sizing(25))

    assert fake.bodies(MARKET)[0] == {
        "ticker": "AAPL_US_EQ", "quantity": 25.0, "extendedHours": False
    }
    assert fake.bodies(STOP)[0] == {
        "ticker": "AAPL_US_EQ", "quantity": -25.0,
        "stopPrice": 96.0, "timeValidity": "GOOD_TILL_CANCEL",
    }
    assert fake.bodies(LIMIT)[0] == {
        "ticker": "AAPL_US_EQ", "quantity": -25.0,
        "limitPrice": 112.0, "timeValidity": "GOOD_TILL_CANCEL",
    }


def test_the_protective_orders_use_the_filled_quantity_not_the_requested_one():
    """A partial fill must be protected for what actually filled."""
    routes = _filled()
    routes[f"GET {ORDERS}/1"] = {"id": 1, "status": "FILLED", "filledQuantity": 18.0}
    fake = FakeT212(routes)
    broker = build(fake, make_settings(t212_tickers="AAPL=AAPL_US_EQ"))
    result = broker.submit_bracket_order(a_signal(), a_sizing(25))

    assert result.qty == 18.0
    assert fake.bodies(STOP)[0]["quantity"] == -18.0


def test_a_short_signal_is_refused_not_silently_skipped():
    fake = FakeT212()
    broker = build(fake, make_settings(t212_tickers="AAPL=AAPL_US_EQ"))
    result = broker.submit_bracket_order(a_signal(direction="short", stop=104, target=88), a_sizing())

    assert not result.accepted
    assert "long only" in result.message
    assert fake.calls == []


def test_zero_size_never_reaches_the_api():
    fake = FakeT212()
    assert not build(fake).submit_bracket_order(a_signal(), a_sizing(0)).accepted
    assert fake.calls == []


def test_a_rejected_stop_is_shouted_about_because_the_position_is_open():
    """
    The worst outcome short of losing the connection entirely: the buy filled
    and the stop did not. The result must say so in terms that prompt action.
    """
    routes = _filled()
    routes[f"POST {STOP}"] = lambda r: httpx.Response(400, text="stop rejected")
    fake = FakeT212(routes)
    broker = build(fake, make_settings(t212_tickers="AAPL=AAPL_US_EQ"))
    result = broker.submit_bracket_order(a_signal(), a_sizing())

    assert result.accepted  # the position IS open — saying otherwise is worse
    assert "STOP WAS REJECTED" in result.message
    assert "manually" in result.message


def test_a_rejected_target_is_only_a_warning():
    routes = _filled()
    routes[f"POST {LIMIT}"] = lambda r: httpx.Response(400, text="nope")
    fake = FakeT212(routes)
    broker = build(fake, make_settings(t212_tickers="AAPL=AAPL_US_EQ"))
    result = broker.submit_bracket_order(a_signal(), a_sizing())

    assert result.accepted
    assert "target order not placed" in result.message
    # The stop still went out, which is the part that matters.
    assert fake.bodies(STOP)


def test_a_rejected_entry_places_nothing_else():
    fake = FakeT212({f"POST {MARKET}": lambda r: httpx.Response(400, text="no funds")})
    broker = build(fake, make_settings(t212_tickers="AAPL=AAPL_US_EQ"))
    result = broker.submit_bracket_order(a_signal(), a_sizing())

    assert not result.accepted
    assert f"POST {STOP}" not in fake.paths()


def test_an_entry_that_never_fills_does_not_get_a_stop():
    routes = _filled()
    routes[f"GET {ORDERS}/1"] = {"id": 1, "status": "CANCELLED", "filledQuantity": 0.0}
    fake = FakeT212(routes)
    broker = build(fake, make_settings(t212_tickers="AAPL=AAPL_US_EQ"))
    result = broker.submit_bracket_order(a_signal(), a_sizing())

    assert not result.accepted
    assert "did not fill" in result.message
    assert f"POST {STOP}" not in fake.paths()


def test_a_fill_that_times_out_is_reported_rather_than_assumed():
    routes = _filled()
    routes[f"GET {ORDERS}/1"] = {"id": 1, "status": "NEW", "filledQuantity": 0.0}
    fake = FakeT212(routes)
    broker = build(
        fake, make_settings(t212_tickers="AAPL=AAPL_US_EQ", t212_fill_timeout=0.01)
    )
    result = broker.submit_bracket_order(a_signal(), a_sizing())

    assert not result.accepted
    assert "did not fill in time" in result.message


# ---------------------------------------------------------------------- #
# Closing
# ---------------------------------------------------------------------- #
OPEN = [
    {
        "instrument": {"ticker": "AAPL_US_EQ"},
        "quantity": 10.0,
        "quantityAvailableForTrading": 10.0,
        "averagePricePaid": 100.0,
        "currentPrice": 105.0,
        "walletImpact": {"unrealizedProfitLoss": 50.0},
    }
]


def test_closing_cancels_the_resting_orders_before_selling():
    """Selling with a stop still live leaves an order against shares that are
    about to be gone."""
    fake = FakeT212(
        {
            f"GET {POSITIONS}": OPEN,
            f"GET {ORDERS}": [
                {"id": 55, "quantity": -10.0, "instrument": {"ticker": "AAPL_US_EQ"}}
            ],
            "DELETE /api/v0/equity/orders/55": {},
            f"POST {MARKET}": {"id": 99, "status": "NEW"},
        }
    )
    broker = build(fake, make_settings(t212_tickers="AAPL=AAPL_US_EQ"))
    result = broker.close_position("AAPL")

    assert result.accepted
    order = fake.paths()
    assert order.index("DELETE /api/v0/equity/orders/55") < order.index(f"POST {MARKET}")
    assert fake.bodies(MARKET)[0]["quantity"] == -10.0


def test_closing_something_not_held_sells_nothing():
    fake = FakeT212({f"GET {POSITIONS}": []})
    broker = build(fake, make_settings(t212_tickers="AAPL=AAPL_US_EQ"))
    result = broker.close_position("AAPL")

    assert not result.accepted
    assert "no open Trading 212 position" in result.message
    assert f"POST {MARKET}" not in fake.paths()


def test_shares_locked_in_a_pie_are_reported_not_sold():
    locked = [dict(OPEN[0], quantityAvailableForTrading=0.0)]
    fake = FakeT212({f"GET {POSITIONS}": locked})
    broker = build(fake, make_settings(t212_tickers="AAPL=AAPL_US_EQ"))
    result = broker.close_position("AAPL")

    assert not result.accepted
    assert "pie" in result.message


# ---------------------------------------------------------------------- #
# reconcile() — the OCO T212 does not provide
# ---------------------------------------------------------------------- #
def test_an_orphaned_sell_order_is_cancelled():
    """
    The target filled, so the position is gone — but its stop is still
    resting. Left alone it is an order to sell shares that no longer exist.
    """
    fake = FakeT212(
        {
            f"GET {POSITIONS}": [],
            f"GET {ORDERS}": [
                {"id": 77, "quantity": -10.0, "instrument": {"ticker": "AAPL_US_EQ"}}
            ],
            "DELETE /api/v0/equity/orders/77": {},
        }
    )
    assert build(fake).reconcile() == {"cancelled": 1, "unprotected": 0}
    assert "DELETE /api/v0/equity/orders/77" in fake.paths()


def test_a_sell_order_for_a_position_still_held_is_left_alone():
    fake = FakeT212(
        {
            f"GET {POSITIONS}": OPEN,
            f"GET {ORDERS}": [
                {"id": 77, "quantity": -10.0, "instrument": {"ticker": "AAPL_US_EQ"}}
            ],
        }
    )
    broker = build(fake, make_settings(t212_tickers="AAPL=AAPL_US_EQ"))
    assert broker.reconcile() == {"cancelled": 0, "unprotected": 0}
    assert not [p for p in fake.paths() if p.startswith("DELETE")]


def test_a_position_with_no_stop_is_counted_as_unprotected():
    """What a crash between the entry and its stop leaves behind."""
    fake = FakeT212({f"GET {POSITIONS}": OPEN, f"GET {ORDERS}": []})
    broker = build(fake, make_settings(t212_tickers="AAPL=AAPL_US_EQ"))
    assert broker.reconcile() == {"cancelled": 0, "unprotected": 1}


def test_a_resting_buy_order_is_none_of_reconciles_business():
    fake = FakeT212(
        {
            f"GET {POSITIONS}": [],
            f"GET {ORDERS}": [
                {"id": 88, "quantity": 5.0, "instrument": {"ticker": "AAPL_US_EQ"}}
            ],
        }
    )
    assert build(fake).reconcile()["cancelled"] == 0


def test_reconcile_survives_the_api_being_down():
    fake = FakeT212({f"GET {POSITIONS}": lambda r: httpx.Response(500, text="boom")})
    assert build(fake).reconcile() == {"cancelled": 0, "unprotected": 0}


# ---------------------------------------------------------------------- #
# Rate limiting
# ---------------------------------------------------------------------- #
def test_repeated_calls_to_the_same_endpoint_are_spaced(monkeypatch):
    slept: list[float] = []
    monkeypatch.setattr("src.broker.t212.time.sleep", lambda s: slept.append(s))

    fake = FakeT212({f"GET {POSITIONS}": []})
    broker = build(fake)
    broker.get_positions()
    broker.get_positions()

    assert slept, "expected the adapter to space repeated calls"
    assert all(0 < s <= 1.2 for s in slept)


def test_a_429_names_the_cause():
    fake = FakeT212({f"GET {POSITIONS}": lambda r: httpx.Response(429, text="slow down")})
    with pytest.raises(BrokerError, match="rate limit"):
        build(fake).get_positions()
