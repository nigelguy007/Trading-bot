"""
FastAPI application entry point.
"""
from __future__ import annotations

import structlog
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from backend.database import init_db
from backend.routes import (
    dashboard_router,
    markets_router,
    trades_router,
    workflow_router,
)
from config.settings import settings

log = structlog.get_logger(__name__)

app = FastAPI(
    title="Prediction Market Trading Bot",
    version="1.0.0",
    description="Claude-powered AI agent system for prediction market trading.",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(dashboard_router, prefix="/api")
app.include_router(markets_router, prefix="/api")
app.include_router(trades_router, prefix="/api")
app.include_router(workflow_router, prefix="/api")


@app.on_event("startup")
async def on_startup() -> None:
    await init_db()
    log.info("app.started")


@app.get("/health")
async def health():
    return {"status": "ok"}
