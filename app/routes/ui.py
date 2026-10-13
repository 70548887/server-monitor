"""Web 面板路由"""

import logging
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Request, Query
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse

from ..services.config import get_config, NodeConfig
from ..services.observability import metrics
from ..services.scheduler import get_scheduler

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/ui", tags=["ui"])

# 模板引擎
templates = Jinja2Templates(directory="app/templates")


def _get_db_repository():
    """获取数据库仓库（如果可用）"""
    try:
        from ..repositories.mysql_repository import get_mysql_repository
        return get_mysql_repository()
    except Exception:
        return None


async def _get_nodes_from_db() -> list:
    """
    从数据库获取节点列表，转换为 NodeConfig 对象
    如果数据库不可用，回退到配置文件
    """
    repo = _get_db_repository()
    if repo:
        try:
            db_nodes = await repo.get_all_nodes(strict=True)
            return [NodeConfig.from_database(n) for n in db_nodes]
        except Exception as e:
            logger.error(f"从数据库获取节点失败: {e}")
    
    # 回退到配置文件
    config = get_config()
    return list(config.nodes)


@router.get("/", response_class=HTMLResponse)
@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request):
    """
    总览仪表盘
    
    展示整体健康状态、节点统计、延迟分布等。
    """
    health = metrics.get_health_status()
    scheduler = get_scheduler()
    
    # 从数据库获取节点列表统计
    all_nodes = await _get_nodes_from_db()
    total_nodes = len(all_nodes)
    enabled_nodes = sum(1 for n in all_nodes if n.enabled)
    
    return templates.TemplateResponse(request,
        "dashboard.html", {
        "request": request,
        "title": "监控面板 - 总览",
        "health": health,
        "scheduler": scheduler.get_status(),
        "stats": {
            "total_nodes": total_nodes,
            "enabled_nodes": enabled_nodes,
            "uptime_seconds": round(metrics.get_uptime_seconds(), 0),
        },
    })


@router.get("/nodes", response_class=HTMLResponse)
async def nodes_list(
    request: Request,
    region: Optional[str] = Query(None),
    tag: Optional[str] = Query(None),
):
    """
    节点列表页面
    从数据库读取节点列表
    """
    # 从数据库获取节点列表
    all_nodes = await _get_nodes_from_db()
    nodes = all_nodes
    
    # 筛选
    if region:
        nodes = [n for n in nodes if n.region == region]
    if tag:
        nodes = [n for n in nodes if tag in n.tags]
    
    # 获取所有区域和标签（用于筛选器）
    all_regions = sorted(set(n.region for n in all_nodes))
    all_tags = sorted(set(t for n in all_nodes for t in n.tags))
    
    return templates.TemplateResponse(request,
        "nodes.html", {
        "request": request,
        "title": "监控面板 - 节点列表",
        "nodes": nodes,
        "filters": {
            "region": region,
            "tag": tag,
        },
        "all_regions": all_regions,
        "all_tags": all_tags,
    })


@router.get("/nodes/{name}", response_class=HTMLResponse)
async def node_detail(request: Request, name: str):
    """
    节点详情页面
    从数据库读取节点信息
    """
    # 从数据库获取节点列表
    all_nodes = await _get_nodes_from_db()
    node = next((n for n in all_nodes if n.name == name), None)
    
    if not node:
        return templates.TemplateResponse(request,
            "error.html", {
            "request": request,
            "title": "节点未找到",
            "error": f"节点 {name} 不存在",
        }, status_code=404)
    
    return templates.TemplateResponse(request,
        "node_detail.html", {
        "request": request,
        "title": f"监控面板 - {name}",
        "node": node,
    })


@router.get("/alerts", response_class=HTMLResponse)
async def alerts_list(request: Request):
    """
    告警页面

    数据由前端 JS 拉取（事件视图 + 告警记录），模板不注入数据。
    """
    return templates.TemplateResponse(
        request,
        "alerts.html",
        {
            "request": request,
            "title": "监控面板 - 告警",
        },
    )


@router.get("/notifications", response_class=HTMLResponse)
async def notifications_page(request: Request):
    """
    通知渠道管理页面
    """
    return templates.TemplateResponse(request,
        "notifications.html", {
        "request": request,
        "title": "监控面板 - 通知渠道",
    })


@router.get("/trends", response_class=HTMLResponse)
async def trends_page(request: Request):
    """
    趋势分析页面
    
    支持节点选择、时间范围选择，展示延迟和可用性趋势对比。
    """
    # 获取所有节点列表供选择
    all_nodes = await _get_nodes_from_db()
    enabled_nodes = [n for n in all_nodes if n.enabled]
    
    return templates.TemplateResponse(request,
        "trends.html", {
        "request": request,
        "title": "监控面板 - 趋势分析",
        "nodes": enabled_nodes,
    })
