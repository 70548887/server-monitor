"""路由模块"""

from .health import router as health_router
from .nodes import router as nodes_router
from .metrics import router as metrics_router
from .ui import router as ui_router

__all__ = [
    "health_router",
    "nodes_router", 
    "metrics_router",
    "ui_router",
]
