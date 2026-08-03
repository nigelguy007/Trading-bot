"""
Wiring.

One place that constructs the whole object graph, so the CLI, the scheduler
and the web server all run against an identically-configured bot.
"""
from __future__ import annotations

from dataclasses import dataclass

from src.ai.analyst import Analyst
from src.ai.client import LLMClient
from src.ai.journal_review import JournalReviewer
from src.alerts.router import AlertRouter
from src.approvals import ApprovalStore
from src.broker.base import Broker
from src.broker.registry import BrokerRouter, build_broker
from src.config import Settings, get_settings
from src.data.market_data import MarketDataProvider, build_market_data
from src.data.news import NewsClient
from src.execution import ExecutionEngine
from src.journal import TradeJournal
from src.logging_setup import configure_logging, get_logger
from src.pipeline import TradingPipeline
from src.portfolio import PortfolioManager
from src.risk import RiskManager
from strategies.base import load_strategies

log = get_logger(__name__)


@dataclass
class AppContext:
    settings: Settings
    journal: TradeJournal
    broker: Broker
    brokers: BrokerRouter
    portfolio: PortfolioManager
    market_data: MarketDataProvider
    news: NewsClient
    llm: LLMClient
    analyst: Analyst
    reviewer: JournalReviewer
    risk: RiskManager
    alerts: AlertRouter
    approvals: ApprovalStore
    execution: ExecutionEngine
    pipeline: TradingPipeline

    def close(self) -> None:
        for closer in (
            self.news.close,
            self.llm.close,
            self.alerts.close,
            self.brokers.close,
            self.journal.close,
        ):
            try:
                closer()
            except Exception:  # pragma: no cover - shutdown must not raise
                log.debug("app.close_failed", closer=getattr(closer, "__qualname__", "?"))
        if hasattr(self.market_data, "close"):
            try:
                self.market_data.close()  # type: ignore[attr-defined]
            except Exception:  # pragma: no cover
                pass


def build_app(
    settings: Settings | None = None,
    *,
    strategies: list[str] | None = None,
    use_debate: bool = False,
    always_analyse: bool = False,
    force_simulated: bool = False,
) -> AppContext:
    settings = settings or get_settings()
    configure_logging(settings.log_level, settings.log_path)

    if settings.live_trading_enabled:
        log.warning(
            "app.LIVE_TRADING_ENABLED",
            note="This process can place REAL orders with REAL money.",
        )

    log.info(
        "app.risk_profile",
        risk_per_trade_pct=settings.risk_per_trade_pct,
        max_position_pct=settings.max_position_pct,
        note=(
            f"stops closer than {settings.implied_min_stop_pct:.1f}% will be "
            "size-capped and risk less than the per-trade budget"
        ),
    )

    journal = TradeJournal(settings.journal_db)
    broker = build_broker(settings, force_simulated=force_simulated)
    brokers = BrokerRouter(broker)
    portfolio = PortfolioManager(broker, settings, journal)
    # `force_simulated` means "run entirely locally" — a simulated broker that
    # still fetches live prices would fail on the first missing key.
    market_data = build_market_data(settings, force_csv=force_simulated)
    news = NewsClient(settings)
    llm = LLMClient(settings)
    analyst = Analyst(llm, settings)
    reviewer = JournalReviewer(llm)
    risk = RiskManager(settings, portfolio)
    alerts = AlertRouter(settings)
    approvals = ApprovalStore(
        settings.data_path / "approvals.json", settings.approval_ttl_minutes
    )
    execution = ExecutionEngine(settings, brokers, journal, approvals, alerts)

    pipeline = TradingPipeline(
        settings=settings,
        market_data=market_data,
        news=news,
        analyst=analyst,
        strategies=load_strategies(strategies),
        risk=risk,
        portfolio=portfolio,
        execution=execution,
        journal=journal,
        alerts=alerts,
        approvals=approvals,
        use_debate=use_debate,
        always_analyse=always_analyse,
    )

    return AppContext(
        settings=settings,
        journal=journal,
        broker=broker,
        brokers=brokers,
        portfolio=portfolio,
        market_data=market_data,
        news=news,
        llm=llm,
        analyst=analyst,
        reviewer=reviewer,
        risk=risk,
        alerts=alerts,
        approvals=approvals,
        execution=execution,
        pipeline=pipeline,
    )
