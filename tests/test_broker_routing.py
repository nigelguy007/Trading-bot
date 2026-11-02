"""
Which venue takes a signal.

Asset class alone cannot answer this once two share brokers are running: IG
and Trading 212 are both "equity", and "spread bet the index at IG, buy the
share at T212" is the whole reason to run them together. So routing is by
symbol first, asset class second, primary broker last.
"""
from __future__ import annotations

import pytest

from src.broker.base import BrokerError
from src.broker.registry import BrokerRouter
from src.config import Settings
from src.models import Signal

from tests.test_combined_portfolio import StubBroker


def settings_for(**kw) -> Settings:
    kw.setdefault("anthropic_api_key", "test")
    return Settings(_env_file=None, **kw)


def a_signal(symbol: str = "AAPL", asset_class: str = "equity") -> Signal:
    return Signal(
        symbol=symbol, direction="long", entry=100.0, stop=98.0, target=106.0,
        confidence=0.9, reasoning="t", asset_class=asset_class,  # type: ignore[arg-type]
    )


def two_venues(routes: str = "") -> BrokerRouter:
    ig = StubBroker("ig", 5_000)
    t212 = StubBroker("t212", 5_000)
    return BrokerRouter(ig, extra=[t212], settings=settings_for(broker_routes=routes))


# ---------------------------------------------------------------------- #
def test_an_explicit_route_wins():
    router = two_venues("FTSE=ig,AAPL=t212")
    assert router.venue_for("FTSE") == "ig"
    assert router.venue_for("AAPL") == "t212"


def test_routing_is_case_insensitive():
    assert two_venues("aapl=t212").venue_for("AAPL") == "t212"
    assert two_venues("AAPL=t212").venue_for("aapl") == "t212"


def test_an_unrouted_symbol_goes_to_the_primary():
    assert two_venues("AAPL=t212").venue_for("NVDA") == "ig"


def test_a_route_naming_a_broker_that_is_not_running_falls_back():
    """A typo in BROKER_ROUTES must not take the bot down, but it must not
    silently send the trade somewhere unintended without a warning either."""
    router = two_venues("AAPL=alpaca")
    assert router.venue_for("AAPL") == "ig"


def test_for_signal_returns_the_actual_broker():
    router = two_venues("AAPL=t212")
    assert router.for_signal(a_signal("AAPL")).name == "t212"
    assert router.for_signal(a_signal("FTSE")).name == "ig"


# ---------------------------------------------------------------------- #
def test_crypto_goes_to_whichever_venue_handles_it():
    """IG is primary and refuses crypto; Alpaca is configured and takes it."""
    ig = StubBroker("ig", 5_000, asset_classes=("equity",))
    alpaca = StubBroker("alpaca", 5_000, asset_classes=("equity", "crypto"))
    router = BrokerRouter(ig, extra=[alpaca], settings=settings_for())

    assert router.venue_for("BTC/USD", "crypto") == "alpaca"
    assert router.venue_for("AAPL", "equity") == "ig"


def test_crypto_with_nowhere_to_go_is_refused_not_mis_routed():
    ig = StubBroker("ig", 5_000, asset_classes=("equity",))
    t212 = StubBroker("t212", 5_000, asset_classes=("equity",))
    router = BrokerRouter(ig, extra=[t212], settings=settings_for())

    with pytest.raises(BrokerError, match="asset class"):
        router.for_signal(a_signal("BTC/USD", "crypto"))


def test_a_single_broker_router_still_routes_everything_to_it():
    router = BrokerRouter(StubBroker("alpaca", 10_000, asset_classes=("equity", "crypto")))
    assert router.venue_for("AAPL") == "alpaca"
    assert router.names == ["alpaca"]


def test_all_returns_every_venue_once():
    router = two_venues()
    assert sorted(b.name for b in router.all()) == ["ig", "t212"]


# ---------------------------------------------------------------------- #
def test_sizing_uses_the_unit_of_the_venue_the_signal_is_going_to():
    """
    The same equity and the same stop are 50 shares at Trading 212 and
    £2.50 a point at IG. A single global unit would size one of them wrong.
    """
    from src.sizing import calculate_size

    s = settings_for(
        broker="ig", ig_api_key="k", ig_username="u", ig_password="p",
        secondary_broker="t212", t212_api_key="tk",
    )
    signal = a_signal("AAPL")
    signal.entry, signal.stop, signal.target = 100.0, 98.0, 106.0

    at_ig = calculate_size(signal, 10_000.0, s, venue="ig")
    at_t212 = calculate_size(signal, 10_000.0, s, venue="t212")

    assert at_ig.unit == "GBP/point"
    assert at_t212.unit == "shares"
    assert at_t212.shares == 50       # whole shares
    assert at_ig.shares == 50.0       # £50 a point, same arithmetic, other meaning


def test_the_risk_manager_asks_the_router_which_venue():
    from src.portfolio import PortfolioManager
    from src.risk import RiskManager

    s = settings_for(
        broker="ig", ig_api_key="k", ig_username="u", ig_password="p",
        secondary_broker="t212", t212_api_key="tk",
        broker_routes="AAPL=t212,FTSE=ig",
    )
    router = BrokerRouter(
        StubBroker("ig", 5_000), extra=[StubBroker("t212", 5_000)], settings=s
    )
    risk = RiskManager(s, PortfolioManager(router, s), router)

    assert risk.venue_for(a_signal("AAPL")) == "t212"
    assert risk.venue_for(a_signal("FTSE")) == "ig"


def test_a_signal_is_sized_in_the_unit_of_the_venue_that_will_fill_it(tmp_path):
    """End to end through the risk gate, not just the sizing function."""
    from src.portfolio import PortfolioManager
    from src.risk import RiskManager

    s = settings_for(
        data_dir=str(tmp_path), log_dir=str(tmp_path / "l"),
        broker="ig", ig_api_key="k", ig_username="u", ig_password="p",
        secondary_broker="t212", t212_api_key="tk",
        broker_routes="AAPL=t212",
    )
    router = BrokerRouter(
        StubBroker("ig", 5_000), extra=[StubBroker("t212", 5_000)], settings=s
    )
    pm = PortfolioManager(router, s)
    risk = RiskManager(s, pm, router)
    state = pm.snapshot()

    to_t212 = risk.evaluate(a_signal("AAPL"), state)
    to_ig = risk.evaluate(a_signal("FTSE"), state)

    assert to_t212.sizing is not None and to_t212.sizing.unit == "shares"
    assert to_ig.sizing is not None and to_ig.sizing.unit == "GBP/point"
