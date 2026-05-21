"""
APScheduler-based cron runner.
Runs a full trading cycle every 15 minutes during market hours.
Usage: python scripts/scheduler.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import structlog
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from agents.risk_agent import PortfolioState
from orchestrator.workflow import TradingWorkflow

log = structlog.get_logger(__name__)

_portfolio = PortfolioState(
    bankroll=1000.0,
    daily_pnl=0.0,
    open_positions_value=0.0,
    open_position_count=0,
    daily_trades=0,
)


async def scheduled_run() -> None:
    log.info("scheduler.cycle_started")
    workflow = TradingWorkflow(dry_run=True)  # Change to dry_run=False for live
    try:
        result = await workflow.run(portfolio=_portfolio)
        log.info(
            "scheduler.cycle_complete",
            trades_filled=result.trades_filled,
            opportunities=result.opportunities_found,
        )
    except Exception as exc:
        log.error("scheduler.cycle_error", error=str(exc))
    finally:
        await workflow.close()


async def main() -> None:
    scheduler = AsyncIOScheduler()
    # Every 15 minutes, 24/7 (prediction markets are always open)
    scheduler.add_job(
        scheduled_run,
        CronTrigger(minute="*/15"),
        id="trading_cycle",
        max_instances=1,
        coalesce=True,
    )
    scheduler.start()
    log.info("scheduler.started", interval="15min")

    try:
        await asyncio.Event().wait()
    except (KeyboardInterrupt, SystemExit):
        scheduler.shutdown()
        log.info("scheduler.stopped")


if __name__ == "__main__":
    asyncio.run(main())
