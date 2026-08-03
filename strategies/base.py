"""Strategy interface."""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional

from src.models import StrategySetup, TechnicalSnapshot


class Strategy(ABC):
    name: str = "unnamed"
    timeframe: str = "swing"

    @abstractmethod
    def evaluate(self, snapshot: TechnicalSnapshot) -> Optional[StrategySetup]:
        """Return a setup if the rules fire, else None. Must never raise."""

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"<Strategy {self.name}>"


def load_strategies(names: list[str] | None = None) -> list[Strategy]:
    from strategies.mean_reversion import MeanReversion
    from strategies.trend_breakout import TrendBreakout

    available: dict[str, type[Strategy]] = {
        "trend_breakout": TrendBreakout,
        "mean_reversion": MeanReversion,
    }
    if not names:
        return [cls() for cls in available.values()]

    selected = []
    for name in names:
        cls = available.get(name.strip().lower())
        if cls is None:
            raise ValueError(f"unknown strategy {name!r}; available: {sorted(available)}")
        selected.append(cls())
    return selected
