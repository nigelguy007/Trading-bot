"""Domain objects passed between pipeline stages."""
from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal, Optional

Direction = Literal["long", "short"]
Timeframe = Literal["intraday", "swing"]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


@dataclass(frozen=True)
class Candle:
    ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass(frozen=True)
class NewsItem:
    title: str
    source: str
    url: str
    published_at: str
    description: str = ""

    def headline(self) -> str:
        return f"[{self.source}] {self.title}"


@dataclass
class TechnicalSnapshot:
    """Everything the AI needs to reason about price, in one serialisable blob."""

    symbol: str
    as_of: datetime
    last_price: float
    prev_close: float
    change_pct: float
    sma20: float
    sma50: float
    sma200: float
    ema9: float
    ema21: float
    rsi14: float
    atr14: float
    atr_pct: float
    macd: float
    macd_signal: float
    macd_hist: float
    bb_upper: float
    bb_lower: float
    bb_pct: float
    volume: float
    avg_volume20: float
    rel_volume: float
    trend: str
    support_levels: list[float] = field(default_factory=list)
    resistance_levels: list[float] = field(default_factory=list)
    range_high_20: float = 0.0
    range_low_20: float = 0.0

    def to_prompt_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["as_of"] = self.as_of.isoformat()
        return {k: (round(v, 4) if isinstance(v, float) else v) for k, v in d.items()}


@dataclass
class NewsAssessment:
    """Output of the News Analysis prompt."""

    impact: Literal["low", "medium", "high"] = "low"
    summary: str = ""
    catalysts: list[str] = field(default_factory=list)
    noise: list[str] = field(default_factory=list)
    direction_bias: Literal["bullish", "bearish", "neutral"] = "neutral"
    headline_count: int = 0


@dataclass
class StrategySetup:
    """A mechanical, non-AI setup produced by a strategy rule."""

    strategy: str
    symbol: str
    direction: Direction
    entry: float
    stop: float
    target: float
    rationale: str
    timeframe: Timeframe = "swing"


@dataclass
class Signal:
    """A trade idea with a complete risk plan. Never has an implicit stop."""

    symbol: str
    direction: Direction
    entry: float
    stop: float
    target: float
    confidence: float
    reasoning: str
    id: str = field(default_factory=lambda: _new_id("sig"))
    created_at: datetime = field(default_factory=_now)
    timeframe: Timeframe = "swing"
    source: str = "ai"
    invalidation: list[str] = field(default_factory=list)
    news_impact: str = "low"
    risk_notes: str = ""
    asset_class: Literal["equity", "crypto"] = "equity"

    @property
    def risk_per_share(self) -> float:
        return abs(self.entry - self.stop)

    @property
    def reward_per_share(self) -> float:
        return abs(self.target - self.entry)

    @property
    def risk_reward(self) -> float:
        r = self.risk_per_share
        return (self.reward_per_share / r) if r > 0 else 0.0

    def stop_is_coherent(self) -> bool:
        """A long's stop must sit below entry and its target above it (and vice versa)."""
        if self.direction == "long":
            return self.stop < self.entry < self.target
        return self.target < self.entry < self.stop

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["created_at"] = self.created_at.isoformat()
        d["risk_reward"] = round(self.risk_reward, 2)
        return d


@dataclass
class PositionSizing:
    shares: float
    dollar_risk: float
    risk_per_share: float
    notional: float
    account_risk_pct: float
    capped_by: str = ""


@dataclass
class RiskDecision:
    approved: bool
    reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    sizing: Optional[PositionSizing] = None
    halt: bool = False  # a portfolio-level circuit breaker tripped

    @property
    def reason(self) -> str:
        return "; ".join(self.reasons) if self.reasons else "ok"


@dataclass
class Position:
    symbol: str
    qty: float
    avg_entry: float
    market_price: float
    unrealized_pnl: float
    side: Direction = "long"

    @property
    def notional(self) -> float:
        return abs(self.qty) * self.market_price


@dataclass
class Account:
    equity: float
    cash: float
    buying_power: float
    last_equity: float = 0.0

    @property
    def day_pnl(self) -> float:
        return self.equity - self.last_equity if self.last_equity else 0.0

    @property
    def day_pnl_pct(self) -> float:
        return (self.day_pnl / self.last_equity * 100) if self.last_equity else 0.0


@dataclass
class OrderResult:
    accepted: bool
    broker: str
    symbol: str
    qty: float
    side: str
    broker_order_id: str = ""
    status: str = ""
    message: str = ""
    raw: dict[str, Any] = field(default_factory=dict)
    id: str = field(default_factory=lambda: _new_id("ord"))
