from __future__ import annotations

import asyncio
import uuid
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from agents.risk_agent import PortfolioState
from backend.database import (
    MarketSnapshot, TradeRecord, WorkflowRunRecord, get_db
)
from config.settings import settings
from orchestrator.workflow import TradingWorkflow

router = APIRouter(prefix="/workflow", tags=["workflow"])

_active_run: dict[str, Any] = {}


class RunRequest(BaseModel):
    dry_run: bool = True
    bankroll: float = 1000.0
    daily_pnl: float = 0.0
    open_positions_value: float = 0.0


async def _persist_results(run, db: AsyncSession, dry_run: bool) -> None:
    run_record = WorkflowRunRecord(
        run_id=run.run_id,
        started_at=run.started_at,
        total_scanned=run.total_markets_scanned,
        opportunities_found=run.opportunities_found,
        trades_attempted=run.trades_attempted,
        trades_filled=run.trades_filled,
        dry_run=dry_run,
        errors=run.errors,
    )
    db.add(run_record)

    for pr in run.results:
        snapshot = MarketSnapshot(
            market_id=pr.opportunity.market_id,
            platform=pr.opportunity.platform,
            question=pr.opportunity.question,
            yes_price=pr.opportunity.yes_price,
            no_price=pr.opportunity.no_price,
            volume_24h=pr.opportunity.volume_24h,
            liquidity=pr.opportunity.liquidity,
            time_to_resolution_days=pr.opportunity.time_to_resolution_days,
            edge=pr.edge.edge,
            model_prob=pr.edge.model_yes_prob,
            sentiment_score=pr.sentiment.composite_sentiment,
        )
        db.add(snapshot)

        if pr.trade and pr.trade.status == "filled":
            trade_record = TradeRecord(
                order_id=pr.trade.order_id,
                market_id=pr.trade.market_id,
                platform=pr.trade.platform,
                direction=pr.edge.direction,
                size_usd=pr.trade.filled_size_usd,
                fill_price=pr.trade.avg_fill_price,
                fee_usd=pr.trade.fee_usd,
                status=pr.trade.status,
                edge=pr.edge.edge,
                model_prob=pr.edge.model_yes_prob,
                market_prob=pr.opportunity.yes_price,
                confidence=pr.edge.confidence,
                pnl_realized=0.0,
                pnl_unrealized=0.0,
                raw_response=pr.trade.raw_response,
            )
            db.add(trade_record)

    await db.commit()


@router.post("/run")
async def trigger_run(
    req: RunRequest,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
):
    if _active_run.get("running"):
        raise HTTPException(status_code=409, detail="A workflow run is already in progress.")

    run_id = str(uuid.uuid4())
    _active_run["running"] = True
    _active_run["run_id"] = run_id

    async def _run_task():
        workflow = TradingWorkflow(dry_run=req.dry_run)
        portfolio = PortfolioState(
            bankroll=req.bankroll,
            daily_pnl=req.daily_pnl,
            open_positions_value=req.open_positions_value,
            open_position_count=0,
            daily_trades=0,
        )
        try:
            result = await workflow.run(portfolio=portfolio, run_id=run_id)
            await _persist_results(result, db, req.dry_run)
        finally:
            _active_run["running"] = False
            await workflow.close()

    background_tasks.add_task(_run_task)

    return {"run_id": run_id, "status": "started", "dry_run": req.dry_run}


@router.get("/status")
async def run_status():
    return {
        "running": _active_run.get("running", False),
        "run_id": _active_run.get("run_id"),
    }
