"""
Database setup — SQLAlchemy async + Redis cache.
"""
from __future__ import annotations

from datetime import datetime, timezone

import redis.asyncio as aioredis
from sqlalchemy import (
    Boolean, Column, DateTime, Float, Integer, String, Text, JSON
)
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from config.settings import settings

engine = create_async_engine(
    settings.database_url,
    echo=settings.debug,
    pool_size=10,
    max_overflow=20,
)

AsyncSessionLocal = sessionmaker(
    engine, class_=AsyncSession, expire_on_commit=False
)

redis_client: aioredis.Redis | None = None


async def get_redis() -> aioredis.Redis:
    global redis_client
    if redis_client is None:
        redis_client = aioredis.from_url(settings.redis_url, decode_responses=True)
    return redis_client


async def get_db() -> AsyncSession:
    async with AsyncSessionLocal() as session:
        yield session


class Base(DeclarativeBase):
    pass


class TradeRecord(Base):
    __tablename__ = "trades"

    id = Column(Integer, primary_key=True, index=True)
    order_id = Column(String(64), unique=True, index=True)
    market_id = Column(String(128), index=True)
    platform = Column(String(32))
    direction = Column(String(8))
    size_usd = Column(Float)
    fill_price = Column(Float)
    fee_usd = Column(Float)
    status = Column(String(32))
    edge = Column(Float)
    model_prob = Column(Float)
    market_prob = Column(Float)
    confidence = Column(Float)
    pnl_realized = Column(Float, default=0.0)
    pnl_unrealized = Column(Float, default=0.0)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    closed_at = Column(DateTime, nullable=True)
    raw_response = Column(JSON, default=dict)


class MarketSnapshot(Base):
    __tablename__ = "market_snapshots"

    id = Column(Integer, primary_key=True, index=True)
    market_id = Column(String(128), index=True)
    platform = Column(String(32))
    question = Column(Text)
    yes_price = Column(Float)
    no_price = Column(Float)
    volume_24h = Column(Float)
    liquidity = Column(Float)
    time_to_resolution_days = Column(Float)
    edge = Column(Float, nullable=True)
    model_prob = Column(Float, nullable=True)
    sentiment_score = Column(Float, nullable=True)
    scanned_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))


class WorkflowRunRecord(Base):
    __tablename__ = "workflow_runs"

    id = Column(Integer, primary_key=True, index=True)
    run_id = Column(String(64), unique=True, index=True)
    started_at = Column(DateTime)
    total_scanned = Column(Integer, default=0)
    opportunities_found = Column(Integer, default=0)
    trades_attempted = Column(Integer, default=0)
    trades_filled = Column(Integer, default=0)
    dry_run = Column(Boolean, default=True)
    errors = Column(JSON, default=list)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))


async def init_db() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
