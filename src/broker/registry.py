"""
Broker selection and multi-broker routing by asset class.

One entry point, so nothing downstream has to know whether it is talking to
Alpaca or the simulator.
"""
from __future__ import annotations

from src.broker.alpaca import AlpacaBroker
from src.broker.base import Broker, BrokerError
from src.broker.ig import IGBroker
from src.broker.paper import SimulatedBroker
from src.config import Settings
from src.logging_setup import get_logger

log = get_logger(__name__)


def build_broker(settings: Settings, force_simulated: bool = False) -> Broker:
    """
    Construct the broker the settings select. BROKER=auto picks by which keys
    are present, IG first — if someone has gone to the trouble of configuring
    IG, that is the account they mean.
    """
    choice = "simulated" if force_simulated else settings.active_broker

    if choice == "simulated":
        if not force_simulated:
            log.warning(
                "broker.simulated_fallback",
                reason="no broker keys — running against the local simulator",
            )
        return SimulatedBroker(settings)

    if choice == "ig":
        broker: Broker = IGBroker(settings)
        log.warning(
            "broker.ig_spread_betting",
            demo=settings.ig_demo,
            note="IG positions are LEVERAGED spread bets sized in "
                 f"{settings.ig_currency}/point, not shares. "
                 "See docs/ig-spread-betting.md.",
        )
    else:
        broker = AlpacaBroker(settings)

    if settings.live_trading_enabled:
        log.warning(
            "broker.LIVE_MODE",
            broker=choice,
            note="REAL MONEY. Orders placed by this process are real.",
        )
    return broker


class BrokerRouter:
    """
    Routes each signal to a broker that handles its asset class.

    Alpaca covers both equities and crypto; IG covers equities, indices, FX and
    commodities but no crypto at all for UK retail clients. A crypto signal on
    an IG setup is therefore refused here rather than being sent somewhere it
    cannot be filled.
    """

    def __init__(self, default: Broker, overrides: dict[str, Broker] | None = None) -> None:
        self.default = default
        self.overrides = overrides or {}

    def for_asset_class(self, asset_class: str) -> Broker:
        broker = self.overrides.get(asset_class, self.default)
        if not broker.supports(asset_class):
            raise BrokerError(f"no broker configured for asset class {asset_class!r}")
        return broker

    def all(self) -> list[Broker]:
        return list({id(b): b for b in [self.default, *self.overrides.values()]}.values())

    def close(self) -> None:
        for broker in self.all():
            broker.close()
