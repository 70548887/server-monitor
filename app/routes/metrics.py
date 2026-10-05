"""Prometheus /metrics 路由"""

from fastapi import APIRouter
from fastapi.responses import Response

from ..services.observability import metrics

router = APIRouter(tags=["metrics"])


@router.get("/metrics")
async def prometheus_metrics():
    """
    Prometheus 指标端点
    
    返回 Prometheus 格式的指标数据，供 Prometheus 抓取。
    """
    content = metrics.generate_metrics()
    return Response(
        content=content,
        media_type="text/plain; charset=utf-8",
    )
