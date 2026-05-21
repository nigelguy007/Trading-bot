from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from backend.database import MarketSnapshot, TradeRecord, WorkflowRunRecord, get_db

router = APIRouter(prefix="/dashboard", tags=["dashboard"])


@router.get("/summary")
async def dashboard_summary(db: AsyncSession = Depends(get_db)):
    trades_q = await db.execute(
        select(
            func.count(TradeRecord.id).label("total"),
            func.sum(TradeRecord.pnl_realized).label("pnl"),
            func.sum(TradeRecord.size_usd).label("volume"),
        ).where(TradeRecord.status == "filled")
    )
    t = trades_q.one()

    markets_q = await db.execute(
        select(func.count(MarketSnapshot.id)).where(MarketSnapshot.edge.isnot(None))
    )
    markets_with_edge = markets_q.scalar() or 0

    last_run_q = await db.execute(
        select(WorkflowRunRecord).order_by(WorkflowRunRecord.started_at.desc()).limit(1)
    )
    last_run = last_run_q.scalar_one_or_none()

    return {
        "total_trades": t.total or 0,
        "total_pnl_usd": round(float(t.pnl or 0), 2),
        "total_volume_usd": round(float(t.volume or 0), 2),
        "markets_with_edge": markets_with_edge,
        "last_run": {
            "run_id": last_run.run_id,
            "started_at": last_run.started_at.isoformat(),
            "markets_scanned": last_run.total_scanned,
            "trades_filled": last_run.trades_filled,
            "dry_run": last_run.dry_run,
        } if last_run else None,
    }


@router.get("/runs")
async def workflow_runs(limit: int = 20, db: AsyncSession = Depends(get_db)):
    stmt = (
        select(WorkflowRunRecord)
        .order_by(WorkflowRunRecord.started_at.desc())
        .limit(limit)
    )
    result = await db.execute(stmt)
    rows = result.scalars().all()
    return [
        {
            "run_id": r.run_id,
            "started_at": r.started_at.isoformat() if r.started_at else None,
            "total_scanned": r.total_scanned,
            "opportunities_found": r.opportunities_found,
            "trades_attempted": r.trades_attempted,
            "trades_filled": r.trades_filled,
            "dry_run": r.dry_run,
            "errors": r.errors,
        }
        for r in rows
    ]
