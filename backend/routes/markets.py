from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.database import MarketSnapshot, get_db

router = APIRouter(prefix="/markets", tags=["markets"])


@router.get("/")
async def list_markets(
    platform: str | None = None,
    min_edge: float | None = None,
    limit: int = 50,
    db: AsyncSession = Depends(get_db),
):
    stmt = select(MarketSnapshot).order_by(MarketSnapshot.scanned_at.desc())
    if platform:
        stmt = stmt.where(MarketSnapshot.platform == platform)
    if min_edge is not None:
        stmt = stmt.where(MarketSnapshot.edge >= min_edge)
    stmt = stmt.limit(limit)

    result = await db.execute(stmt)
    rows = result.scalars().all()

    return [
        {
            "market_id": r.market_id,
            "platform": r.platform,
            "question": r.question,
            "yes_price": r.yes_price,
            "no_price": r.no_price,
            "volume_24h": r.volume_24h,
            "liquidity": r.liquidity,
            "time_to_resolution_days": r.time_to_resolution_days,
            "edge": r.edge,
            "model_prob": r.model_prob,
            "sentiment_score": r.sentiment_score,
            "scanned_at": r.scanned_at.isoformat() if r.scanned_at else None,
        }
        for r in rows
    ]


@router.get("/{market_id}")
async def get_market(market_id: str, db: AsyncSession = Depends(get_db)):
    stmt = (
        select(MarketSnapshot)
        .where(MarketSnapshot.market_id == market_id)
        .order_by(MarketSnapshot.scanned_at.desc())
        .limit(1)
    )
    result = await db.execute(stmt)
    row = result.scalar_one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="Market not found")
    return row
