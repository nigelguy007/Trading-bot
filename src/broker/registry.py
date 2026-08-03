"""
Broker selection and multi-broker routing by asset class.

One entry point, so nothing downstream has to know whether it is talking to
Alpaca or the simulator.
"""
from __future__ import annotations

from src.broker.alpaca import AlpacaBroker
from src.broker.base import Broker, BrokerError
from src.broker.paper import SimulatedBroker
from src.config import Settings
from src.logging_setup import get_logger

log = get_logger(__name__)


def build_broker(settings: Settings, force_simulated: bool = False) -> Broker:
    if force_simulated or not settings.has_broker_keys:
        if not force_simulated:
            log.warning(
                "broker.simulated_fallback",
                reason="no Alpaca keys — running against the local simulator",
            )
        return SimulatedBroker(settings)

    broker = AlpacaBroker(settings)
    if settings.live_trading_enabled:
        log.warning(
            "broker.LIVE_MODE",
            note="REAL MONEY. Orders placed by this process are real.",
        )
    return broker


class BrokerRouter:
    """
    Routes each signal to a broker that handles its asset class.

    Today Alpaca covers both equities and crypto, so this is a single-entry
    table — but it is the seam where a second broker gets added without
    touching the execution engine.
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
