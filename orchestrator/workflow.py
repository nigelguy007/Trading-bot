"""
Main Trading Workflow Orchestrator
Runs the full pipeline: Scan → Sentiment → Edge → Risk → Execute
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import structlog

from agents.edge_detector import EdgeDetectorAgent, EdgeResult
from agents.execution_agent import ExecutionAgent, TradeResult
from agents.market_scanner import MarketScannerAgent, MarketOpportunity
from agents.risk_agent import RiskAgent, PortfolioState, RiskDecision
from agents.sentiment_agent import SentimentAgent, SentimentSignal
from models.probability_model import ProbabilityModel
from services.kalshi_api import KalshiService
from services.polymarket_api import PolymarketService
from services.reddit_scraper import RedditScraper
from services.twitter_scraper import TwitterScraper

log = structlog.get_logger(__name__)


@dataclass
class PipelineResult:
    opportunity: MarketOpportunity
    sentiment: SentimentSignal
    edge: EdgeResult
    risk: RiskDecision
    trade: TradeResult | None
    duration_ms: float
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class WorkflowRun:
    run_id: str
    started_at: datetime
    total_markets_scanned: int
    opportunities_found: int
    trades_attempted: int
    trades_filled: int
    results: list[PipelineResult] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


class TradingWorkflow:
    def __init__(
        self,
        dry_run: bool = True,
        max_concurrent_markets: int = 3,
    ) -> None:
        self.dry_run = dry_run
        self.max_concurrent = max_concurrent_markets

        # Services
        self.polymarket = PolymarketService()
        self.kalshi = KalshiService()
        self.twitter = TwitterScraper()
        self.reddit = RedditScraper()

        # Agents
        self.scanner = MarketScannerAgent()
        self.sentiment_agent = SentimentAgent()
        self.edge_agent = EdgeDetectorAgent()
        self.risk_agent = RiskAgent()
        self.execution_agent = ExecutionAgent(
            polymarket_service=self.polymarket,
            kalshi_service=self.kalshi,
        )

        self.prob_model = ProbabilityModel()

    async def _fetch_all_markets(self) -> list[dict[str, Any]]:
        """Fetch markets from all platforms concurrently."""
        results = await asyncio.gather(
            self.polymarket.get_markets(),
            self.kalshi.get_markets(),
            return_exceptions=True,
        )
        all_markets: list[dict] = []
        for r in results:
            if isinstance(r, Exception):
                log.error("workflow.market_fetch_error", error=str(r))
            else:
                all_markets.extend(r)
        return all_markets

    async def _gather_sentiment(
        self, opp: MarketOpportunity
    ) -> SentimentSignal:
        keywords = opp.question[:100]
        twitter_posts, reddit_posts = await asyncio.gather(
            self.twitter.search(keywords, max_results=30),
            self.reddit.search(keywords, limit=20),
            return_exceptions=False,
        )
        return self.sentiment_agent.analyse(
            market_id=opp.market_id,
            question=opp.question,
            market_yes_prob=opp.yes_price,
            twitter_posts=twitter_posts if isinstance(twitter_posts, list) else [],
            reddit_posts=reddit_posts if isinstance(reddit_posts, list) else [],
            news_items=[],
        )

    async def _process_opportunity(
        self,
        opp: MarketOpportunity,
        portfolio: PortfolioState,
    ) -> PipelineResult:
        t0 = datetime.now(timezone.utc)

        sentiment = await self._gather_sentiment(opp)

        calibration = self.prob_model.calibrate(opp.yes_price, sentiment)

        edge = self.edge_agent.detect(opp, sentiment)

        # Blend model estimate with calibration
        blended_prob = self.prob_model.blend(
            opp.yes_price,
            edge.model_yes_prob,
            calibration.posterior,
        )
        edge.model_yes_prob = blended_prob
        edge.edge = blended_prob - opp.yes_price

        risk = self.risk_agent.evaluate(edge, portfolio)

        trade: TradeResult | None = None
        if risk.approved:
            trade = await self.execution_agent.execute(
                edge=edge,
                risk=risk,
                platform=opp.platform,
                market_yes_price=opp.yes_price,
                dry_run=self.dry_run,
            )
            if trade and trade.status == "filled":
                portfolio.open_positions_value += risk.position_size_usd
                portfolio.daily_trades += 1

        duration = (datetime.now(timezone.utc) - t0).total_seconds() * 1000

        log.info(
            "workflow.opportunity_processed",
            market_id=opp.market_id,
            platform=opp.platform,
            edge=round(edge.edge, 4),
            approved=risk.approved,
            trade_status=trade.status if trade else "none",
            duration_ms=round(duration),
        )

        return PipelineResult(
            opportunity=opp,
            sentiment=sentiment,
            edge=edge,
            risk=risk,
            trade=trade,
            duration_ms=duration,
        )

    async def run(
        self,
        portfolio: PortfolioState,
        run_id: str | None = None,
    ) -> WorkflowRun:
        import uuid

        run_id = run_id or str(uuid.uuid4())
        started = datetime.now(timezone.utc)
        log.info("workflow.run_started", run_id=run_id, dry_run=self.dry_run)

        all_markets = await self._fetch_all_markets()
        scan_result = self.scanner.scan(all_markets)

        log.info(
            "workflow.scan_complete",
            total_scanned=scan_result.total_scanned,
            opportunities=len(scan_result.opportunities),
        )

        pipeline_results: list[PipelineResult] = []
        errors: list[str] = []

        sem = asyncio.Semaphore(self.max_concurrent)

        async def _guarded(opp: MarketOpportunity) -> PipelineResult | None:
            async with sem:
                try:
                    return await self._process_opportunity(opp, portfolio)
                except Exception as exc:
                    log.error("workflow.pipeline_error", market_id=opp.market_id, error=str(exc))
                    errors.append(f"{opp.market_id}: {exc}")
                    return None

        tasks = [_guarded(opp) for opp in scan_result.opportunities]
        raw_results = await asyncio.gather(*tasks)

        for r in raw_results:
            if r is not None:
                pipeline_results.append(r)

        trades_filled = sum(
            1
            for r in pipeline_results
            if r.trade and r.trade.status == "filled"
        )

        run = WorkflowRun(
            run_id=run_id,
            started_at=started,
            total_markets_scanned=scan_result.total_scanned,
            opportunities_found=len(scan_result.opportunities),
            trades_attempted=sum(1 for r in pipeline_results if r.trade),
            trades_filled=trades_filled,
            results=pipeline_results,
            errors=errors,
        )

        log.info(
            "workflow.run_complete",
            run_id=run_id,
            trades_filled=trades_filled,
            duration_s=round((datetime.now(timezone.utc) - started).total_seconds(), 1),
        )

        return run

    async def close(self) -> None:
        await asyncio.gather(
            self.polymarket.close(),
            self.kalshi.close(),
            self.twitter.close(),
            self.reddit.close(),
        )
