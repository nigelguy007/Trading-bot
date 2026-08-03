"""
End-to-end pipeline test with a stubbed model.

Proves the chain actually connects: candles -> indicators -> strategy trigger
-> AI setup -> risk gate -> sizing -> journal -> approval queue, and that a
halt stops the whole thing before any model call is made.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from src.ai.analyst import Analyst
from src.ai.client import LLMResponse
from src.approvals import ApprovalStore
from src.broker.paper import SimulatedBroker
from src.broker.registry import BrokerRouter
from src.config import Settings
from src.data.market_data import MarketDataProvider
from src.execution import ExecutionEngine
from src.journal import TradeJournal
from src.models import Candle
from src.pipeline import TradingPipeline
from src.portfolio import PortfolioManager
from src.risk import RiskManager
from strategies.base import load_strategies


# --- doubles ------------------------------------------------------------ #
class FakeLLM:
    """Returns a scripted setup; records what it was asked."""

    model = "fake-model"

    def __init__(self, setup: dict | None = None, refuse: bool = False) -> None:
        self.setup = setup
        self.refuse = refuse
        self.calls: list[str] = []

    def complete(self, system, user, *, schema=None, max_tokens=None, effort=None):
        self.calls.append(user[:60])
        if self.refuse:
            return LLMResponse(text="", refused=True, refusal_category="test")
        if schema is not None and "impact" in json.dumps(schema):
            return LLMResponse(
                text="{}",
                parsed={
                    "impact": "low", "summary": "quiet", "catalysts": [],
                    "noise": [], "direction_bias": "neutral",
                },
            )
        if schema is not None:
            return LLMResponse(text="{}", parsed=self.setup)
        return LLMResponse(text="prose analysis")

    def close(self): ...


class FakeMarketData(MarketDataProvider):
    def __init__(self, candles: list[Candle]) -> None:
        self.candles = candles

    def get_candles(self, symbol, timeframe="1Day", limit=250):
        return self.candles[-limit:]


class FakeNews:
    def get_headlines(self, symbol, limit=10):
        return []

    def close(self): ...


class SilentAlerts:
    def __init__(self) -> None:
        self.sent: list[str] = []

    def signal(self, *a, **kw): self.sent.append("signal")
    def rejection(self, *a, **kw): self.sent.append("rejection")
    def halt(self, *a, **kw): self.sent.append("halt")
    def fill(self, *a, **kw): self.sent.append("fill")
    def broadcast(self, *a, **kw): self.sent.append("broadcast")


def breakout_series(n: int = 260) -> list[Candle]:
    """Trends up with volume spikes, so the breakout rule fires."""
    start = datetime(2023, 1, 1, tzinfo=timezone.utc)
    out, price = [], 100.0
    for i in range(n):
        cycle = i % 10
        if cycle < 5:
            price *= 1.003
            volume = 1_000_000
        elif cycle < 8:
            price *= 0.996
            volume = 700_000
        else:
            price *= 1.008
            volume = 3_000_000
        out.append(
            Candle(ts=start + timedelta(days=i), open=price * 0.998, high=price * 1.008,
                   low=price * 0.992, close=price, volume=volume)
        )
    return out


GOOD_SETUP = {
    "has_setup": True,
    "direction": "long",
    "entry": 100.0,
    "stop": 96.0,
    "target": 112.0,
    "confidence": 0.85,
    "reasoning": "breaking the range high on volume",
    "invalidation": ["closes back under 96"],
    "risks": "gap risk overnight",
    "no_trade_reason": "",
}


@pytest.fixture
def make_pipeline(tmp_path):
    def _make(setup: dict | None = GOOD_SETUP, **overrides):
        settings = Settings(
            anthropic_api_key="test",
            data_dir=str(tmp_path),
            watchlist="AAPL",
            execution_mode="alert",
            _env_file=None,
            **overrides,
        )
        llm = FakeLLM(setup)
        broker = SimulatedBroker(settings, starting_equity=10_000)
        broker.reset(10_000)
        journal = TradeJournal(tmp_path / "j.db")
        portfolio = PortfolioManager(broker, settings, journal)
        approvals = ApprovalStore(tmp_path / "a.json", 45)
        alerts = SilentAlerts()
        execution = ExecutionEngine(
            settings, BrokerRouter(broker), journal, approvals, alerts
        )
        pipeline = TradingPipeline(
            settings=settings,
            market_data=FakeMarketData(breakout_series()),
            news=FakeNews(),
            analyst=Analyst(llm, settings),
            strategies=load_strategies(),
            risk=RiskManager(settings, portfolio),
            portfolio=portfolio,
            execution=execution,
            journal=journal,
            alerts=alerts,
            approvals=approvals,
        )
        return pipeline, llm, journal, approvals, alerts, broker

    return _make


# --- tests --------------------------------------------------------------- #
def test_full_chain_produces_a_pending_approval(make_pipeline):
    pipeline, llm, journal, approvals, alerts, broker = make_pipeline()

    result = pipeline.run_cycle(["AAPL"])

    assert result.scanned == 1
    assert result.analysed == 1
    assert result.signals == 1
    assert result.approved == 1
    assert result.pending == 1
    assert not result.errors

    pending = approvals.pending()
    assert len(pending) == 1
    assert pending[0]["signal"]["symbol"] == "AAPL"
    assert pending[0]["sizing"]["shares"] > 0
    # Alert mode: the broker has seen nothing.
    assert broker.get_positions() == []
    assert "signal" in alerts.sent


def test_approving_the_signal_reaches_the_broker(make_pipeline):
    pipeline, _, journal, approvals, _, broker = make_pipeline()
    pipeline.run_cycle(["AAPL"])

    signal_id = approvals.pending()[0]["signal"]["id"]
    ok, message = pipeline.execution.approve(signal_id)

    assert ok, message
    assert len(broker.get_positions()) == 1
    assert journal.get_signal(signal_id)["status"] == "submitted"
    assert len(journal.open_trades()) == 1


def test_no_setup_means_no_signal(make_pipeline):
    no_setup = dict(GOOD_SETUP, has_setup=False, no_trade_reason="chop")
    pipeline, _, _, approvals, _, _ = make_pipeline(no_setup)

    result = pipeline.run_cycle(["AAPL"])

    assert result.analysed == 1
    assert result.signals == 0
    assert approvals.pending() == []


def test_incoherent_levels_are_dropped_before_the_risk_gate(make_pipeline):
    """A long with its stop above entry never becomes a signal at all."""
    broken = dict(GOOD_SETUP, stop=105.0)
    pipeline, _, _, approvals, _, _ = make_pipeline(broken)

    result = pipeline.run_cycle(["AAPL"])

    assert result.signals == 0
    assert approvals.pending() == []


def test_poor_risk_reward_is_journalled_as_rejected(make_pipeline):
    poor = dict(GOOD_SETUP, target=101.0)  # 4 risk for 1 reward
    pipeline, _, journal, approvals, alerts, broker = make_pipeline(poor)

    result = pipeline.run_cycle(["AAPL"])

    assert result.signals == 1
    assert result.rejected == 1
    assert result.approved == 0
    assert approvals.pending() == []
    assert broker.get_positions() == []

    stored = journal.recent_signals(1)[0]
    assert stored["risk_approved"] == 0
    assert "R:R" in stored["risk_reason"]
    assert "rejection" in alerts.sent


def test_a_refusal_does_not_break_the_cycle(make_pipeline):
    pipeline, llm, _, _, _, _ = make_pipeline()
    llm.refuse = True

    result = pipeline.run_cycle(["AAPL"])

    assert result.signals == 0
    assert not result.errors


def test_drawdown_halt_stops_before_any_model_call(make_pipeline):
    pipeline, llm, journal, _, alerts, broker = make_pipeline()
    # Bank a peak, then crater equity well past the 15% limit.
    pipeline.portfolio.snapshot()
    broker.state["cash"] = 5_000.0
    broker._save()

    result = pipeline.run_cycle(["AAPL"])

    assert result.halted
    assert "MAX DRAWDOWN" in result.halt_reason
    assert llm.calls == []          # not a single token spent
    assert "halt" in alerts.sent


def test_run_is_recorded_in_the_journal(make_pipeline):
    pipeline, _, journal, _, _, _ = make_pipeline()
    pipeline.run_cycle(["AAPL"])

    row = journal.conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 1").fetchone()
    assert row["scanned"] == 1
    assert row["signals"] == 1
    assert row["finished_at"] is not None


def test_one_broken_symbol_does_not_end_the_cycle(make_pipeline):
    pipeline, _, _, _, _, _ = make_pipeline()

    class Exploding(FakeMarketData):
        def get_candles(self, symbol, timeframe="1Day", limit=250):
            if symbol == "BOOM":
                raise RuntimeError("data source on fire")
            return super().get_candles(symbol, timeframe, limit)

    pipeline.market_data = Exploding(breakout_series())
    result = pipeline.run_cycle(["BOOM", "AAPL"])

    assert result.scanned == 2
    assert result.signals == 1          # AAPL still got through
    assert len(result.errors) == 1
