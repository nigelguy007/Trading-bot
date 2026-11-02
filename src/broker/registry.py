"""
Broker selection and routing.

One entry point, so nothing downstream has to know whether it is talking to
Alpaca, IG, Trading 212 or the simulator.

Routing happens at three levels, most specific first:

  1. BROKER_ROUTES names a venue for a symbol — "FTSE=ig,AAPL=t212".
  2. Asset class, for the one split the venues decide themselves: only Alpaca
     handles crypto, so a crypto signal goes there whatever else is running.
  3. The primary broker.

Level 1 exists because the obvious combination needs it. IG and Trading 212
are both "equity" as far as the signal is concerned, so asset class cannot
tell them apart — and "spread bet the index at IG, buy the share at T212" is
precisely the reason to run two brokers at all.
"""
from __future__ import annotations

from src.broker.alpaca import AlpacaBroker
from src.broker.base import Broker, BrokerError
from src.broker.ig import IGBroker
from src.broker.paper import SimulatedBroker
from src.broker.t212 import Trading212Broker
from src.config import Settings
from src.logging_setup import get_logger

log = get_logger(__name__)

BUILDERS = {
    "alpaca": AlpacaBroker,
    "ig": IGBroker,
    "t212": Trading212Broker,
    "simulated": SimulatedBroker,
}


def build_one(name: str, settings: Settings) -> Broker:
    """Construct a single named adapter."""
    try:
        builder = BUILDERS[name]
    except KeyError:
        raise BrokerError(
            f"unknown broker {name!r}; expected one of {sorted(BUILDERS)}"
        ) from None

    broker = builder(settings)
    if name == "ig":
        log.warning(
            "broker.ig_spread_betting",
            demo=settings.ig_demo,
            note="IG positions are LEVERAGED spread bets sized in "
                 f"{settings.ig_currency}/point, not shares. "
                 "See docs/ig-spread-betting.md.",
        )
    elif name == "t212":
        log.warning(
            "broker.t212_no_brackets",
            demo=settings.t212_demo,
            note="Trading 212 has NO bracket orders: the stop is placed as a "
                 "separate order AFTER the entry fills, so there is a brief "
                 "unprotected window. See docs/trading-212.md.",
        )
    if settings.live_enabled_for(name):
        log.warning(
            "broker.LIVE_MODE",
            broker=name,
            note="REAL MONEY. Orders placed by this process are real.",
        )
    return broker


def build_broker(settings: Settings, force_simulated: bool = False) -> Broker:
    """The primary broker. BROKER=auto picks by which keys are present."""
    choice = "simulated" if force_simulated else settings.active_broker
    if choice == "simulated" and not force_simulated:
        log.warning(
            "broker.simulated_fallback",
            reason="no broker keys — running against the local simulator",
        )
    return build_one(choice, settings)


def build_router(settings: Settings, force_simulated: bool = False) -> "BrokerRouter":
    """
    Build every configured venue and wire the routing table.

    With one broker this is just `BrokerRouter(primary)`. With two it is the
    object that makes "combined portfolio" mean something: the portfolio layer
    asks it for every broker, not just the one that happens to be first.
    """
    primary = build_broker(settings, force_simulated=force_simulated)
    if force_simulated or not settings.multi_broker:
        return BrokerRouter(primary, settings=settings)

    secondary_name = settings.secondary_broker
    try:
        secondary = build_one(secondary_name, settings)
    except BrokerError as exc:
        # A broken second broker must not take the whole bot down — but it
        # must not be silently ignored either, because the routes pointing at
        # it would quietly fall through to the primary.
        log.error(
            "broker.secondary_unavailable",
            broker=secondary_name,
            error=str(exc),
            note="running on the primary broker alone; routes naming the "
                 "secondary will fall back, which may not be what you want",
        )
        return BrokerRouter(primary, settings=settings)

    log.info(
        "broker.multi_venue",
        primary=primary.name,
        secondary=secondary.name,
        routes=settings.route_map or "none — everything goes to the primary",
    )
    return BrokerRouter(primary, extra=[secondary], settings=settings)


class BrokerRouter:
    """
    Decides which venue handles a signal, and owns the full set of brokers.

    `all()` is what makes combined risk accounting possible: the portfolio
    layer sums across everything this returns rather than reading one account
    and calling it the portfolio.
    """

    def __init__(
        self,
        default: Broker,
        overrides: dict[str, Broker] | None = None,
        *,
        extra: list[Broker] | None = None,
        settings: Settings | None = None,
    ) -> None:
        self.default = default
        self.overrides = overrides or {}   # asset class -> broker
        self.settings = settings
        self._by_name: dict[str, Broker] = {default.name: default}
        for broker in [*(extra or []), *self.overrides.values()]:
            self._by_name.setdefault(broker.name, broker)

    # ------------------------------------------------------------------ #
    def by_name(self, name: str) -> Broker | None:
        return self._by_name.get(name)

    def venue_for(self, symbol: str, asset_class: str = "equity") -> str:
        """Which venue NAME should take this symbol. Pure, so it is testable."""
        routes = self.settings.route_map if self.settings else {}
        wanted = routes.get(symbol.upper())
        if wanted and wanted in self._by_name:
            return wanted
        if wanted:
            log.warning(
                "broker.route_target_missing",
                symbol=symbol,
                wanted=wanted,
                available=sorted(self._by_name),
                note="falling back — check BROKER_ROUTES and SECONDARY_BROKER agree",
            )

        if asset_class in self.overrides:
            return self.overrides[asset_class].name

        # Asset-class fallback: if the default cannot hold this asset class but
        # another configured venue can, use that rather than failing. This is
        # what sends crypto to Alpaca when IG is primary.
        if not self.default.supports(asset_class):
            for name, broker in self._by_name.items():
                if broker.supports(asset_class):
                    return name
        return self.default.name

    def for_symbol(self, symbol: str, asset_class: str = "equity") -> Broker:
        broker = self._by_name[self.venue_for(symbol, asset_class)]
        if not broker.supports(asset_class):
            raise BrokerError(
                f"no broker configured for asset class {asset_class!r} "
                f"(symbol {symbol}); {broker.name} does not handle it"
            )
        return broker

    def for_signal(self, signal) -> Broker:
        return self.for_symbol(signal.symbol, signal.asset_class)

    def for_asset_class(self, asset_class: str) -> Broker:
        """Kept for callers that have no symbol to hand."""
        broker = self.overrides.get(asset_class, self.default)
        if not broker.supports(asset_class):
            for candidate in self._by_name.values():
                if candidate.supports(asset_class):
                    return candidate
            raise BrokerError(f"no broker configured for asset class {asset_class!r}")
        return broker

    def all(self) -> list[Broker]:
        return list(self._by_name.values())

    @property
    def names(self) -> list[str]:
        return list(self._by_name)

    def close(self) -> None:
        for broker in self.all():
            broker.close()
