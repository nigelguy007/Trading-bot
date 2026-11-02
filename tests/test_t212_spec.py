"""
The adapter checked against Trading 212's own OpenAPI document.

This exists because the adapter is hand-written. The reason for writing it by
hand is sound — the hard parts (emulating a bracket, repairing the missing
OCO, rate limiting so a 429 cannot land between an entry and its stop) are
bot logic that no generic client provides — but it carries one real risk a
generated SDK would not have: the hand-mapped field names can be wrong, or
drift when T212 changes the API.

So the spec is vendored at tests/fixtures/t212-openapi.json and every request
body the adapter sends, and every response field it reads, is checked against
it. A field renamed upstream fails a test here instead of an order there.

Refresh the fixture with:

    curl -o tests/fixtures/t212-openapi.json https://docs.trading212.com/_bundle/api.json
"""
from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from src.broker.t212 import RATE_LIMITS, Trading212Broker

from tests.test_t212_broker import (  # noqa: F401  (no_sleeping is an autouse fixture)
    FakeT212,
    LIMIT,
    MARKET,
    ORDERS,
    POSITIONS,
    STOP,
    SUMMARY,
    _filled,
    a_signal,
    a_sizing,
    build,
    make_settings,
    no_sleeping,
)

SPEC_PATH = Path(__file__).parent / "fixtures" / "t212-openapi.json"


@pytest.fixture(scope="module")
def spec() -> dict:
    return json.loads(SPEC_PATH.read_text(encoding="utf-8"))


def deref(node: dict, spec: dict) -> dict:
    """Resolve a single $ref one level."""
    ref = node.get("$ref")
    if not ref:
        return node
    target = spec
    for part in ref.lstrip("#/").split("/"):
        target = target[part]
    return target


def request_properties(spec: dict, path: str, method: str = "post") -> set[str]:
    body = spec["paths"][path][method]["requestBody"]
    schema = deref(body["content"]["application/json"]["schema"], spec)
    return set(schema.get("properties", {}))


def response_properties(spec: dict, path: str, method: str = "get") -> set[str]:
    content = spec["paths"][path][method]["responses"]["200"]["content"]
    schema = deref(content["application/json"]["schema"], spec)
    if schema.get("type") == "array":
        schema = deref(schema["items"], spec)
    return set(schema.get("properties", {}))


# ---------------------------------------------------------------------- #
def test_the_vendored_spec_is_the_real_thing(spec):
    assert spec["info"]["title"] == "Trading 212 Public API"
    assert spec["openapi"].startswith("3.")


def test_every_path_the_adapter_calls_exists_in_the_spec(spec):
    """Catches a typo'd or removed endpoint without touching the network."""
    declared = set(spec["paths"])
    used = {
        SUMMARY,
        POSITIONS,
        ORDERS,
        MARKET,
        STOP,
        LIMIT,
        "/api/v0/equity/metadata/instruments",
        "/api/v0/equity/orders/{id}",
    }
    assert used <= declared, f"not in the spec: {sorted(used - declared)}"


def test_the_rate_limit_table_covers_what_the_adapter_calls(spec):
    """
    A missing entry silently falls back to the default gap, which for the
    one-request-per-50-seconds instruments endpoint would mean a 429.
    """
    for path in (SUMMARY, POSITIONS, ORDERS, MARKET, STOP, LIMIT):
        assert path in RATE_LIMITS, f"{path} has no client-side rate limit"


# ---------------------------------------------------------------------- #
# Request bodies
# ---------------------------------------------------------------------- #
def _bodies_sent() -> dict[str, dict]:
    """Run a full entry/stop/target sequence and return what went on the wire."""
    fake = FakeT212(_filled())
    broker = build(fake, make_settings(t212_tickers="AAPL=AAPL_US_EQ"))
    broker.submit_bracket_order(a_signal(), a_sizing(25))
    return {
        MARKET: fake.bodies(MARKET)[0],
        STOP: fake.bodies(STOP)[0],
        LIMIT: fake.bodies(LIMIT)[0],
    }


@pytest.mark.parametrize("path", [MARKET, STOP, LIMIT])
def test_order_bodies_use_only_fields_the_spec_declares(spec, path):
    sent = _bodies_sent()[path]
    allowed = request_properties(spec, path)
    unknown = set(sent) - allowed
    assert not unknown, f"{path} sent fields not in the spec: {sorted(unknown)}"


def test_the_stop_order_carries_the_fields_that_make_it_a_stop(spec):
    sent = _bodies_sent()[STOP]
    assert {"ticker", "quantity", "stopPrice"} <= set(sent)
    assert {"ticker", "quantity", "stopPrice"} <= request_properties(spec, STOP)


def test_the_limit_order_carries_a_limit_price(spec):
    sent = _bodies_sent()[LIMIT]
    assert "limitPrice" in sent
    assert "limitPrice" in request_properties(spec, LIMIT)


def test_time_validity_values_are_ones_the_spec_allows(spec):
    allowed = set(spec["components"]["schemas"]["TimeValidity"]["enum"])
    for path in (STOP, LIMIT):
        assert _bodies_sent()[path]["timeValidity"] in allowed


def test_the_market_order_has_no_side_field_because_the_spec_has_none(spec):
    """
    The sign of `quantity` is the side. If a future spec adds a real `side`
    field this test is the prompt to stop relying on the sign.
    """
    assert "side" not in request_properties(spec, MARKET)
    assert "side" not in _bodies_sent()[MARKET]


# ---------------------------------------------------------------------- #
# Response fields
# ---------------------------------------------------------------------- #
def test_account_fields_the_adapter_reads_exist(spec):
    schemas = spec["components"]["schemas"]
    assert set(response_properties(spec, SUMMARY)) >= {"cash", "investments"}
    assert {"availableToTrade", "reservedForOrders", "inPies"} <= set(
        schemas["Cash"]["properties"]
    )
    assert "currentValue" in schemas["Investments"]["properties"]


def test_position_fields_the_adapter_reads_exist(spec):
    props = response_properties(spec, POSITIONS)
    assert {
        "instrument",
        "quantity",
        "averagePricePaid",
        "currentPrice",
        "quantityAvailableForTrading",
        "walletImpact",
    } <= props

    schemas = spec["components"]["schemas"]
    assert "ticker" in schemas["Instrument"]["properties"]
    assert "unrealizedProfitLoss" in schemas["PositionWalletImpact"]["properties"]


def test_order_fields_the_adapter_reads_exist(spec):
    props = response_properties(spec, "/api/v0/equity/orders/{id}")
    assert {"id", "status", "filledQuantity", "quantity", "instrument"} <= props


def test_the_statuses_the_adapter_treats_as_terminal_are_real(spec):
    from src.broker.t212 import TERMINAL

    declared = set(spec["components"]["schemas"]["Order"]["properties"]["status"]["enum"])
    assert TERMINAL <= declared, f"invented statuses: {sorted(TERMINAL - declared)}"
    assert "FILLED" in declared


def test_instrument_listing_fields_the_adapter_reads_exist(spec):
    props = response_properties(spec, "/api/v0/equity/metadata/instruments")
    assert {"ticker", "shortName"} <= props


# ---------------------------------------------------------------------- #
def test_parsing_a_spec_shaped_response_produces_a_sane_position(spec):
    """
    An end-to-end check that the mapping is right, built from the spec's own
    field names rather than from what the adapter happens to expect.
    """
    position_fields = response_properties(spec, POSITIONS)
    row = {
        "instrument": {"ticker": "AAPL_US_EQ", "currency": "USD", "name": "Apple"},
        "quantity": 7.0,
        "averagePricePaid": 200.0,
        "currentPrice": 210.0,
        "quantityAvailableForTrading": 7.0,
        "walletImpact": {"unrealizedProfitLoss": 55.0, "currency": "GBP"},
    }
    assert set(row) <= position_fields

    fake = FakeT212({f"GET {POSITIONS}": [row]})
    (parsed,) = build(fake).get_positions()

    assert parsed.symbol == "AAPL"
    assert parsed.qty == 7.0
    assert parsed.avg_entry == 200.0
    assert parsed.market_price == 210.0
    assert parsed.unrealized_pnl == 55.0
    assert parsed.venue == "t212"
