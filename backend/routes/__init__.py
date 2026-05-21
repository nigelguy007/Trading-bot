from .markets import router as markets_router
from .trades import router as trades_router
from .dashboard import router as dashboard_router
from .workflow import router as workflow_router

__all__ = ["markets_router", "trades_router", "dashboard_router", "workflow_router"]
