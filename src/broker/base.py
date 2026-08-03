"""Broker interface."""
from __future__ import annotations

from abc import ABC, abstractmethod

from src.models import Account, OrderResult, Position, PositionSizing, Signal


class BrokerError(RuntimeError):
    pass


class Broker(ABC):
    name: str = "unnamed"

    @property
    def is_paper(self) -> bool:
        return True

    @abstractmethod
    def get_account(self) -> Account: ...

    @abstractmethod
    def get_positions(self) -> list[Position]: ...

    @abstractmethod
    def submit_bracket_order(self, signal: Signal, sizing: PositionSizing) -> OrderResult:
        """Submit entry + stop + target as one bracket. Never submit a naked entry."""

    @abstractmethod
    def close_position(self, symbol: str) -> OrderResult: ...

    def is_market_open(self) -> bool:
        return True

    def supports(self, asset_class: str) -> bool:
        return asset_class in ("equity", "crypto")

    def close(self) -> None:  # pragma: no cover - most brokers hold no resources
        return None
