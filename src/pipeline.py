"""
The pipeline — one full cycle of the automation workflow.

    market scan (watchlist)
        -> chart + news analysis (AI)
        -> signal generation      (only when the setup is clean)
        -> risk check + sizing    (the gate: AI never reaches the broker)
        -> journal                (everything, including rejections)
        -> alert                  (with reasoning, awaiting approval)
        -> daily report

Two things are deliberate:

* The circuit breakers run once, before any symbol is touched. If the daily
  loss limit or max drawdown has tripped, the cycle stops before spending a
  single token — the cheapest possible response to "stop trading today".
* Strategies pre-filter. A symbol only reaches the model when a mechanical rule
  fires or `always_analyse` is on, so token spend tracks opportunity.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from src.ai.analyst import Analyst
from src.ai.client import LLMError
from src.ai.debate import AgentDebate
from src.alerts.router import AlertRouter
from src.approvals import ApprovalStore
from src.broker.registry import BrokerRouter
from src.config import Settings
from src.data.indicators import build_snapshot
from src.data.market_data import MarketDataError, MarketDataProvider
from src.data.news import NewsClient
from src.execution import ExecutionEngine
from src.journal import TradeJournal
from src.logging_setup import get_logger
from src.models import Signal, TechnicalSnapshot
from src.portfolio import PortfolioManager
from src.reports import build_daily_report
from src.risk import RiskManager
from strategies.base import Strategy

log = get_logger(__name__)


@dataclass
class CycleResult:
    scanned: int = 0
    analysed: int = 0
    signals: int = 0
    approved: int = 0
    submitted: int = 0
    pending: int = 0
    rejected: int = 0
    errors: list[str] = field(default_factory=list)
    halted: bool = False
    halt_reason: str = ""

    def summary(self) -> str:
        if self.halted:
            return f"HALTED — {self.halt_reason}"
        return (
            f"scanned {self.scanned}, analysed {self.analysed}, signals {self.signals}, "
            f"approved {self.approved}, pending {self.pending}, submitted {self.submitted}, "
            f"rejected {self.rejected}, errors {len(self.errors)}"
        )


class TradingPipeline:
    def __init__(
        self,
        settings: Settings,
        market_data: MarketDataProvider,
        news: NewsClient,
        analyst: Analyst,
        strategies: list[Strategy],
        risk: RiskManager,
        portfolio: PortfolioManager,
        execution: ExecutionEngine,
        journal: TradeJournal,
        alerts: AlertRouter,
        approvals: ApprovalStore,
        *,
        use_debate: bool = False,
        always_analyse: bool = False,
    ) -> None:
        self.settings = settings
        self.market_data = market_data
        self.news = news
        self.analyst = analyst
        self.strategies = strategies
        self.risk = risk
        self.portfolio = portfolio
        self.execution = execution
        self.journal = journal
        self.alerts = alerts
        self.approvals = approvals
        self.use_debate = use_debate
        self.always_analyse = always_analyse
        self.debate = AgentDebate(analyst.llm) if use_debate else None

    # ------------------------------------------------------------------ #
    def run_cycle(self, symbols: list[str] | None = None) -> CycleResult:
        symbols = symbols or self.settings.all_symbols
        result = CycleResult()
        run_id = self.journal.start_run()

        expired = self.execution.expire_stale_approvals()
        if expired:
            log.info("pipeline.approvals_expired", count=expired)

        state = self.portfolio.snapshot()

        # --- Circuit breakers, before anything expensive ---
        breaker = self.risk.check_circuit_breakers(state)
        if not breaker.approved:
            result.halted = True
            result.halt_reason = breaker.reason
            self.alerts.halt(breaker, state)
            self.journal.finish_run(run_id, 0, 0, 0, 0, notes=breaker.reason)
            log.error("pipeline.halted", reason=breaker.reason)
            return result
        for warning in breaker.warnings:
            log.warning("pipeline.risk_warning", warning=warning)

        # --- Scan ---
        for symbol in symbols:
            result.scanned += 1
            try:
                self._process_symbol(symbol, state, result)
            except MarketDataError as exc:
                result.errors.append(f"{symbol}: {exc}")
                log.warning("pipeline.market_data_error", symbol=symbol, error=str(exc))
            except LLMError as exc:
                result.errors.append(f"{symbol}: {exc}")
                log.warning("pipeline.llm_error", symbol=symbol, error=str(exc))
            except Exception as exc:  # one bad symbol must not end the cycle
                result.errors.append(f"{symbol}: {exc}")
                log.exception("pipeline.symbol_failed", symbol=symbol)

        self.journal.finish_run(
            run_id,
            scanned=result.scanned,
            signals=result.signals,
            approved=result.approved,
            errors=len(result.errors),
            notes=result.summary(),
        )
        log.info("pipeline.cycle_complete", summary=result.summary())
        return result

    # ------------------------------------------------------------------ #
    def _process_symbol(self, symbol: str, state, result: CycleResult) -> None:
        candles = self.market_data.get_candles(symbol, "1Day", limit=250)
        snapshot = build_snapshot(symbol, candles)
        if snapshot is None:
            log.info("pipeline.insufficient_history", symbol=symbol)
            return

        # Mechanical pre-filter: is this even worth a model call?
        setups = [s for s in (st.evaluate(snapshot) for st in self.strategies) if s]
        if not setups and not self.always_analyse:
            log.debug("pipeline.no_strategy_trigger", symbol=symbol, trend=snapshot.trend)
            return

        result.analysed += 1
        headlines = self.news.get_headlines(symbol)
        news = self.analyst.news_analysis(symbol, headlines)

        signal = self._generate_signal(snapshot, news, setups)
        if signal is None:
            return

        result.signals += 1
        decision = self.risk.evaluate(signal, state)

        if not decision.approved:
            result.rejected += 1
            self.execution.handle_signal(signal, decision)
            return

        result.approved += 1
        status = self.execution.handle_signal(signal, decision)
        if status == "submitted":
            result.submitted += 1
        elif status == "pending_approval":
            result.pending += 1

    def _generate_signal(self, snapshot: TechnicalSnapshot, news, setups) -> Signal | None:
        """
        A strategy trigger says "look here"; the AI decides whether there is
        actually a trade. Both have to agree, which is the point — the
        mechanical rule can't see the news, and the model can't be trusted to
        stay disciplined on its own.
        """
        if self.debate is not None:
            signal, transcript = self.debate.run(snapshot, news)
            if signal is not None and transcript:
                signal.risk_notes = (
                    signal.risk_notes
                    + "\n\nStrongest counterargument: "
                    + transcript.get("strongest_counterargument", "n/a")
                ).strip()
            return signal

        signal = self.analyst.find_setup(snapshot, news, timeframe="swing")
        if signal is None:
            return None

        # Record which mechanical rule pointed us here, for per-strategy stats.
        if setups:
            signal.source = f"ai+{setups[0].strategy}"
        return signal

    # ------------------------------------------------------------------ #
    def daily_report(self) -> str:
        state = self.portfolio.snapshot()
        broker = self.execution.brokers.default
        report = build_daily_report(
            self.journal,
            state,
            paper=broker.is_paper,
            execution_mode=self.settings.execution_mode,
        )
        self.alerts.broadcast(report)
        log.info("pipeline.daily_report_sent", at=datetime.now(timezone.utc).isoformat())
        return report
