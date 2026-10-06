"""健康探针路由 - P0 监控自身健康"""

import time
import logging
from datetime import datetime

from fastapi import APIRouter, Response
from fastapi.responses import JSONResponse

from ..services.observability import metrics
from ..services.scheduler import get_scheduler
from ..utils.timeutil import utcnow

logger = logging.getLogger(__name__)

router = APIRouter(tags=["health"])


@router.get("/healthz")
async def health_check():
    """
    综合健康检查
    
    返回监控面板整体健康状态，包含各组件状态。
    - healthy: 所有组件正常
    - degraded: 部分组件异常但核心功能可用
    - unhealthy: 核心组件异常，服务不可用
    """
    health_status = metrics.get_health_status()
    
    # 根据状态返回对应 HTTP 状态码
    if health_status["status"] == "healthy":
        return JSONResponse(content=health_status, status_code=200)
    elif health_status["status"] == "degraded":
        return JSONResponse(content=health_status, status_code=200)
    else:
        return JSONResponse(content=health_status, status_code=503)


@router.get("/healthz/live")
async def liveness_probe():
    """
    存活探针 (Liveness Probe)
    
    检测进程是否存活。只要进程运行即返回成功。
    用于 Kubernetes 等编排系统判断是否需要重启容器。
    """
    return {
        "status": "alive",
        "timestamp": utcnow().isoformat() + "Z",
    }


@router.get("/healthz/ready")
async def readiness_probe():
    """
    就绪探针 (Readiness Probe)
    
    检测服务是否就绪可接收流量。
    需要存储层和调度器都就绪才返回成功。
    """
    health_status = metrics.get_health_status()
    scheduler = get_scheduler()
    
    # 检查调度器状态
    scheduler_ready = scheduler.is_running
    
    # 检查存储层状态（允许 unknown，因为可能还未执行 ping）
    mysql_ready = health_status["components"]["mysql"]["status"] != "down"
    
    is_ready = scheduler_ready and mysql_ready
    
    response = {
        "ready": is_ready,
        "timestamp": utcnow().isoformat() + "Z",
        "checks": {
            "scheduler": scheduler_ready,
            "mysql": mysql_ready,
        },
    }
    
    if is_ready:
        return JSONResponse(content=response, status_code=200)
    else:
        return JSONResponse(content=response, status_code=503)


@router.get("/healthz/scheduler")
async def scheduler_health():
    """
    调度器健康检查
    
    返回调度器详细状态，包含：
    - 运行状态
    - 最后执行时间
    - 执行次数
    - 异常次数
    """
    scheduler = get_scheduler()
    status = scheduler.get_status()
    
    return {
        "timestamp": utcnow().isoformat() + "Z",
        **status,
    }


@router.post("/healthz/ping-storage")
async def ping_storage():
    """
    手动触发存储层健康检测
    
    执行 MySQL 和 Redis ping，更新指标并返回结果。
    """
    results = {}
    
    # Ping MySQL (异步)
    try:
        from ..models.base import ping_database
        mysql_ok, mysql_latency = await ping_database()
        metrics.record_storage_ping("mysql", mysql_ok, mysql_latency)
        results["mysql"] = {
            "status": "up" if mysql_ok else "down",
            "latency_ms": round(mysql_latency, 2),
        }
    except Exception as e:
        logger.error(f"MySQL ping 错误: {e}")
        metrics.record_storage_ping("mysql", False, 0)
        results["mysql"] = {
            "status": "error",
            "error": str(e),
        }
    
    # Ping Redis (异步)
    try:
        from ..services.config import get_config
        config = get_config()
        if config.database.redis_host:
            from ..repositories.redis_repository import get_redis_repository
            redis_repo = get_redis_repository()
            redis_ok, redis_latency = await redis_repo.ping()
            metrics.record_storage_ping("redis", redis_ok, redis_latency)
            results["redis"] = {
                "status": "up" if redis_ok else "down",
                "latency_ms": round(redis_latency, 2),
            }
    except Exception as e:
        logger.error(f"Redis ping 错误: {e}")
        metrics.record_storage_ping("redis", False, 0)
        results["redis"] = {
            "status": "down",
            "error": str(e),
        }
    
    return {
        "timestamp": utcnow().isoformat() + "Z",
        "results": results,
    }


@router.get("/healthz/database")
async def database_health():
    """
    数据库详细状态
    
    返回数据库连接状态，包含：
    - 连接是否可用
    - 最近的错误信息
    - 当前延迟
    """
    from ..models.base import get_db_status, ping_database
    
    db_status = get_db_status()
    
    # 尝试 ping 一下获取最新状态
    try:
        ping_ok, ping_latency = await ping_database()
    except Exception as e:
        ping_ok = False
        ping_latency = 0
        logger.error(f"数据库 ping 失败: {e}")
    
    response = {
        "timestamp": utcnow().isoformat() + "Z",
        "available": db_status["available"] and ping_ok,
        "last_error": db_status["last_error"],
        "ping": {
            "success": ping_ok,
            "latency_ms": round(ping_latency, 2),
        },
    }
    
    status_code = 200 if response["available"] else 503
    return JSONResponse(content=response, status_code=status_code)


@router.post("/healthz/database/reconnect")
async def database_reconnect():
    """
    手动触发数据库重连
    
    当数据库连接出现问题时，可以调用此接口手动触发重连。
    """
    from ..models.base import reconnect_db, get_db_status
    
    logger.info("Manual database reconnect triggered")
    
    success = await reconnect_db()
    db_status = get_db_status()
    
    response = {
        "timestamp": utcnow().isoformat() + "Z",
        "reconnect_success": success,
        "available": db_status["available"],
        "last_error": db_status["last_error"],
    }
    
    status_code = 200 if success else 503
    return JSONResponse(content=response, status_code=status_code)
