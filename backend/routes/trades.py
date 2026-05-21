from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from backend.database import TradeRecord, get_db

router = APIRouter(prefix="/trades", tags=["trades"])


@router.get("/")
async def list_trades(
    status: str | None = None,
    platform: str | None = None,
    limit: int = 100,
    db: AsyncSession = Depends(get_db),
):
    stmt = select(TradeRecord).order_by(TradeRecord.created_at.desc())
    if status:
        stmt = stmt.where(TradeRecord.status == status)
    if platform:
        stmt = stmt.where(TradeRecord.platform == platform)
    stmt = stmt.limit(limit)

    result = await db.execute(stmt)
    rows = result.scalars().all()
    return [_trade_dict(r) for r in rows]


@router.get("/stats")
async def trade_stats(db: AsyncSession = Depends(get_db)):
    total_q = await db.execute(select(func.count(TradeRecord.id)))
    filled_q = await db.execute(
        select(func.count(TradeRecord.id)).where(TradeRecord.status == "filled")
    )
    pnl_q = await db.execute(
        select(func.sum(TradeRecord.pnl_realized)).where(TradeRecord.status == "filled")
    )
    vol_q = await db.execute(
        select(func.sum(TradeRecord.size_usd)).where(TradeRecord.status == "filled")
    )

    return {
        "total_trades": total_q.scalar() or 0,
        "filled_trades": filled_q.scalar() or 0,
        "total_pnl_realized": round(float(pnl_q.scalar() or 0), 2),
        "total_volume_usd": round(float(vol_q.scalar() or 0), 2),
    }


@router.get("/{order_id}")
async def get_trade(order_id: str, db: AsyncSession = Depends(get_db)):
    stmt = select(TradeRecord).where(TradeRecord.order_id == order_id)
    result = await db.execute(stmt)
    row = result.scalar_one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="Trade not found")
    return _trade_dict(row)


def _trade_dict(r: TradeRecord) -> dict:
    return {
        "order_id": r.order_id,
        "market_id": r.market_id,
        "platform": r.platform,
        "direction": r.direction,
        "size_usd": r.size_usd,
        "fill_price": r.fill_price,
        "fee_usd": r.fee_usd,
        "status": r.status,
        "edge": r.edge,
        "model_prob": r.model_prob,
        "market_prob": r.market_prob,
        "confidence": r.confidence,
        "pnl_realized": r.pnl_realized,
        "pnl_unrealized": r.pnl_unrealized,
        "created_at": r.created_at.isoformat() if r.created_at else None,
    }
