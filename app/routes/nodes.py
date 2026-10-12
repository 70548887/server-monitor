"""节点管理路由 - CRUD 接口"""

import logging
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import JSONResponse

from ..schemas.node import NodeConfig, NodeStatus, NodeCreate, NodeUpdate, NodeExpect
from ..services.config import get_config, reload_nodes
from ..services.scheduler import get_scheduler
from ..services.availability import get_availability_service, AvailabilityService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/nodes", tags=["nodes"])

# 内存中的节点状态缓存
_node_status_cache: dict[str, NodeStatus] = {}


def _get_db_repository():
    """获取数据库仓库（如果可用）"""
    try:
        from ..repositories.mysql_repository import get_mysql_repository
        return get_mysql_repository()
    except Exception:
        return None


async def _persist_node(method: str, *args, **kwargs):
    repo = _get_db_repository()
    if repo is None:
        raise HTTPException(status_code=503, detail="数据库不可用，节点未保存")
    try:
        result = await getattr(repo, method)(*args, **kwargs)
    except Exception as exc:
        logger.warning("节点持久化失败: %s", type(exc).__name__)
        raise HTTPException(status_code=503, detail="数据库操作失败，节点未保存") from exc
    if not result:
        raise HTTPException(status_code=503, detail="数据库操作失败，节点未保存")
    return result


async def _db_create_node(node_data: dict):
    return await _persist_node("create_node", node_data)


async def _db_update_node(name: str, update_data: dict):
    return await _persist_node("update_node", name, update_data)


async def _db_delete_node(name: str, hard: bool):
    return await _persist_node("delete_node", name, hard=hard)


def _get_node_status(name: str) -> Optional[NodeStatus]:
    """获取节点状态"""
    return _node_status_cache.get(name)


def _update_node_status(name: str, status: NodeStatus):
    """更新节点状态"""
    _node_status_cache[name] = status


@router.get("", response_model=list[NodeStatus])
async def list_nodes(
    region: Optional[str] = Query(None, description="按区域筛选"),
    tag: Optional[str] = Query(None, description="按标签筛选"),
    enabled: Optional[bool] = Query(None, description="按启用状态筛选"),
):
    """
    获取所有节点列表及其状态
    
    支持按 region、tag、enabled 筛选。
    从数据库读取节点列表。
    """
    # 从数据库获取节点列表
    repo = _get_db_repository()
    if repo:
        try:
            db_nodes = await repo.get_all_nodes()
        except Exception as e:
            logger.error(f"从数据库获取节点失败: {e}")
            db_nodes = []
    else:
        # 数据库不可用，回退到配置文件
        config = get_config()
        db_nodes = [
            {
                "name": n.name,
                "url": n.url,
                "region": n.region,
                "tags": n.tags,
                "enabled": n.enabled,
            }
            for n in config.nodes
        ]
    
    results = []
    for node in db_nodes:
        # 筛选条件
        if region and node.get("region") != region:
            continue
        if tag and tag not in node.get("tags", []):
            continue
        if enabled is not None and node.get("enabled") != enabled:
            continue
        
        node_name = node["name"]
        # 获取缓存的状态或创建默认状态
        cached = _get_node_status(node_name)
        if cached:
            results.append(cached)
        else:
            results.append(NodeStatus(
                name=node_name,
                url=node["url"],
                region=node.get("region", "default"),
                tags=node.get("tags", []),
                enabled=node.get("enabled", True),
            ))
    
    return results


# ============= 可用性统计 API (静态路由需在动态路由之前) =============

@router.get("/stats/availability")
async def get_all_availability():
    """
    获取所有节点的可用性统计
    
    返回所有节点的24h/7d/30d可用性概览。
    """
    service = get_availability_service()
    all_stats = service.get_all_stats()
    repo = _get_db_repository()
    agg_7d, agg_30d = {}, {}
    if repo is not None:
        try:
            agg_7d = await repo.get_aggregated_uptime(days=7)
            agg_30d = await repo.get_aggregated_uptime(days=30)
        except Exception:
            logger.exception("读取总览聚合统计失败")
    
    results = []
    for name in sorted(set(all_stats) | set(agg_7d) | set(agg_30d)):
        stats = service.get_stats(name)
        results.append({
            "node_name": name,
            "uptime_24h": stats.uptime_24h,
            "uptime_7d": agg_7d.get(name, {}).get("uptime"),
            "uptime_30d": agg_30d.get(name, {}).get("uptime"),
            "coverage_7d": agg_7d.get(name),
            "coverage_30d": agg_30d.get(name),
            "avg_latency_ms": stats.avg_latency_ms,
            "current_status": stats.current_status,
            "last_check_at": stats.last_check_at.isoformat() + "Z" if stats.last_check_at else None,
        })
    
    return {
        "total": len(results),
        "nodes": results,
    }


@router.get("/stats/latency-trend")
async def get_all_latency_trends(
    hours: int = Query(24, ge=1, le=168, description="查询小时数 (1-168)"),
):
    """
    获取所有节点的延迟趋势数据
    
    用于绘制曲线图，返回每小时的平均/最大/最小延迟和可用性。
    只返回当前数据库中存在的节点，已删除节点不会显示。
    """
    service = get_availability_service()
    all_trends = service.get_all_latency_trends(hours)
    
    # 获取当前数据库中存在的节点列表
    repo = _get_db_repository()
    existing_nodes = set()
    if repo:
        try:
            nodes = await repo.get_all_nodes(include_deleted=False)
            existing_nodes = {n["name"] for n in nodes}
        except Exception as e:
            logger.error(f"从数据库获取节点失败: {e}")
    
    # 过滤，只返回当前存在的节点
    filtered_trends = {}
    for node_name, trend in all_trends.items():
        if node_name in existing_nodes:
            filtered_trends[node_name] = trend
    
    return {
        "hours": hours,
        "nodes": filtered_trends,
    }


@router.get("/{name}")
async def get_node(name: str):
    """
    获取单个节点详情
    从数据库读取节点信息。
    """
    # 从数据库查找节点
    repo = _get_db_repository()
    node = None
    if repo:
        try:
            node = await repo.get_node_by_name(name)
        except Exception as e:
            logger.error(f"从数据库获取单个节点失败: {e}")
    
    if not node:
        raise HTTPException(
            status_code=404,
            detail={"code": "E003", "message": f"Node not found: {name}"},
        )
    
    # 直接返回数据库字典，包含所有字段（expect, timeout_ms等）
    # 这样前端编辑功能可以正确填充所有配置项
    return node


@router.post("", response_model=NodeStatus, status_code=201)
async def create_node(node: NodeCreate):
    """
    创建新的监控节点
    
    节点数据持久化到数据库。
    """
    # 从数据库检查名称唯一性
    repo = _get_db_repository()
    if repo:
        try:
            existing = await repo.get_node_by_name(node.name)
            if existing:
                raise HTTPException(
                    status_code=409,
                    detail={"code": "E002", "message": f"Node already exists: {node.name}"},
                )
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"检查节点是否存在失败: {e}")
    
    # 持久化到数据库
    await _db_create_node({
        "name": node.name,
        "url": node.url,
        "region": node.region,
        "tags": node.tags,
        "timeout_ms": node.timeout_ms,
        "enabled": node.enabled,
        "expect": {
            "status": node.expect.status if node.expect else 200,
            "body_contains": node.expect.body_contains if node.expect else None,
        },
    })
    
    logger.info(f"节点已创建: {node.name}")
    
    # 返回状态
    status = NodeStatus(
        name=node.name,
        url=node.url,
        region=node.region,
        tags=node.tags,
        enabled=node.enabled,
    )
    _update_node_status(node.name, status)
    
    return status


@router.put("/{name}", response_model=NodeStatus)
async def update_node(name: str, update: NodeUpdate):
    """
    更新监控节点配置
    从数据库读取并更新节点。
    """
    # 从数据库查找节点
    repo = _get_db_repository()
    node = None
    if repo:
        try:
            node = await repo.get_node_by_name(name)
        except Exception as e:
            logger.error(f"从数据库获取单个节点失败: {e}")
    
    if not node:
        raise HTTPException(
            status_code=404,
            detail={"code": "E003", "message": f"Node not found: {name}"},
        )
    
    # 构建更新数据
    update_data = {}
    if update.url is not None:
        update_data["url"] = update.url
    if update.region is not None:
        update_data["region"] = update.region
    if update.tags is not None:
        update_data["tags"] = update.tags
    if update.timeout_ms is not None:
        update_data["timeout_ms"] = update.timeout_ms
    if update.enabled is not None:
        update_data["enabled"] = update.enabled
    if update.expect is not None:
        update_data["expect"] = {
            "status": update.expect.status,
            "body_contains": update.expect.body_contains,
        }
    
    # 持久化到数据库
    await _db_update_node(name, update_data)
    logger.info(f"Node updated: {name}")
    
    # 更新缓存状态
    cached = _get_node_status(name)
    if cached:
        if update.url is not None:
            cached.url = update.url
        if update.region is not None:
            cached.region = update.region
        if update.tags is not None:
            cached.tags = update.tags
        if update.enabled is not None:
            cached.enabled = update.enabled
    else:
        cached = NodeStatus(
            name=name,
            url=update.url or node.get("url", ""),
            region=update.region or node.get("region", "default"),
            tags=update.tags or node.get("tags", []),
            enabled=update.enabled if update.enabled is not None else node.get("enabled", True),
        )
        _update_node_status(name, cached)
    
    return cached


@router.delete("/{name}")
async def delete_node(name: str, hard: bool = Query(False, description="是否硬删除")):
    """
    删除监控节点
    
    - soft delete (默认): 仅禁用节点
    - hard delete: 从数据库中移除
    """
    # 从数据库查找节点
    repo = _get_db_repository()
    node = None
    if repo:
        try:
            node = await repo.get_node_by_name(name)
        except Exception as e:
            logger.error(f"从数据库获取单个节点失败: {e}")
    
    if not node:
        raise HTTPException(
            status_code=404,
            detail={"code": "E003", "message": f"Node not found: {name}"},
        )
    
    # 持久化到数据库
    await _db_delete_node(name, hard=hard)
    
    if hard:
        # 硬删除 - 清除缓存
        if name in _node_status_cache:
            del _node_status_cache[name]
        # 清除可用性服务中的历史数据
        availability_service = get_availability_service()
        availability_service.remove_node(name)
        logger.info(f"Node hard deleted: {name}")
    else:
        # 软删除（禁用）- 更新缓存
        if name in _node_status_cache:
            _node_status_cache[name].enabled = False
        logger.info(f"Node soft deleted (disabled): {name}")
    
    return {"message": f"Node {'deleted' if hard else 'disabled'}: {name}"}


@router.post("/{name}/ping")
async def ping_node(name: str):
    """
    手动触发单个节点的心跳检测
    
    立即执行一次检测并返回结果。
    """
    # 从数据库查找节点
    repo = _get_db_repository()
    node = None
    if repo:
        try:
            node = await repo.get_node_by_name(name)
        except Exception as e:
            logger.error(f"从数据库获取单个节点失败: {e}")
    
    if not node:
        raise HTTPException(
            status_code=404,
            detail={"code": "E003", "message": f"Node not found: {name}"},
        )
    
    # 执行心跳检测
    from ..services.heartbeat import HeartbeatService
    from ..services.config import NodeConfig as ConfigNode, NodeExpectConfig
    
    service = HeartbeatService()
    
    try:
        result = await service.check_node(ConfigNode(
            name=node["name"],
            url=node["url"],
            region=node.get("region", "default"),
            tags=node.get("tags", []),
            timeout_ms=node.get("timeout_ms", 2000),
            enabled=True,
            expect=NodeExpectConfig(
                status=node.get("expect_status", 200),
                body_contains=node.get("expect_body_contains"),
            ),
        ))
        
        # 更新缓存状态
        cached = _get_node_status(name) or NodeStatus(
            name=node["name"],
            url=node["url"],
            region=node.get("region", "default"),
            tags=node.get("tags", []),
            enabled=node.get("enabled", True),
        )
        cached.last_status_code = result.status_code
        cached.last_latency_ms = result.latency_ms
        cached.last_error = result.error_message
        cached.last_check_at = result.timestamp
        if result.success:
            cached.consecutive_failures = 0
        else:
            cached.consecutive_failures += 1
        _update_node_status(name, cached)
        
        return {
            "node": name,
            "timestamp": result.timestamp.isoformat() + "Z",
            "status_code": result.status_code,
            "latency_ms": round(result.latency_ms, 2),
            "success": result.success,
            "error_type": result.error_type,
            "error_message": result.error_message,
        }
    
    finally:
        await service.close()


@router.post("/reload")
async def reload_node_config():
    """
    热重载节点配置
    
    从 nodes.yaml 文件重新加载节点列表。
    """
    try:
        nodes = reload_nodes()
        return {
            "message": "Nodes reloaded successfully",
            "count": len(nodes),
        }
    except Exception as e:
        logger.error(f"重新加载节点失败: {e}")
        raise HTTPException(
            status_code=500,
            detail={"code": "E006", "message": f"Failed to reload: {str(e)}"},
        )


# 供其他模块更新节点状态
def update_node_status_from_result(name: str, result, node_info: dict = None):
    """
    从心跳结果更新节点状态缓存
    
    node_info: 可选的节点信息字典，用于初始化缓存
    """
    cached = _get_node_status(name)
    
    if not cached:
        # 使用传入的节点信息创建缓存
        if node_info:
            cached = NodeStatus(
                name=name,
                url=node_info.get("url", ""),
                region=node_info.get("region", "default"),
                tags=node_info.get("tags", []),
                enabled=node_info.get("enabled", True),
            )
        else:
            # 没有节点信息，创建最小化缓存
            cached = NodeStatus(
                name=name,
                url="",
                region="default",
                tags=[],
                enabled=True,
            )
    
    cached.last_status_code = result.status_code
    cached.last_latency_ms = result.latency_ms
    cached.last_error = result.error_message
    cached.last_check_at = result.timestamp
    
    if result.success:
        cached.consecutive_failures = 0
    else:
        cached.consecutive_failures += 1
    
    _update_node_status(name, cached)


# ============= 可用性统计 API =============

@router.get("/{name}/availability")
async def get_node_availability(name: str):
    """
    获取节点可用性统计

    返回 24h/7d/30d 可用性百分比。

    注意 7d/30d 的口径：内存只保留 48 小时记录，因此这两个数字
    必须从聚合表读取；聚合表不可用（未迁移或未生成）时，
    返回 None 而不是"用 48 小时数据算出来的 7 天值"——
    后者看起来正常但其实是静默失真。
    """
    # 从数据库查找节点
    repo = _get_db_repository()
    node = None
    if repo:
        try:
            node = await repo.get_node_by_name(name)
        except Exception as e:
            logger.error(f"从数据库获取单个节点失败: {e}")

    if not node:
        raise HTTPException(
            status_code=404,
            detail={"code": "E003", "message": f"Node not found: {name}"},
        )

    service = get_availability_service()
    stats = service.get_stats(name)

    # 长周期可用性来自聚合表（内存无法覆盖 7 天/30 天）
    uptime_7d = None
    uptime_30d = None
    long_range_source = "unavailable"
    agg_7d, agg_30d = {}, {}
    if repo:
        try:
            agg_7d = await repo.get_aggregated_uptime(node_name=name, days=7)
            if name in agg_7d:
                uptime_7d = agg_7d[name]["uptime"]
            agg_30d = await repo.get_aggregated_uptime(node_name=name, days=30)
            if name in agg_30d:
                uptime_30d = agg_30d[name]["uptime"]
            if uptime_7d is not None or uptime_30d is not None:
                long_range_source = "aggregates"
        except Exception as e:
            logger.warning(f"读取聚合可用性失败，长周期指标将不可用: {e}")

    # 聚合表尚无数据时，仅在内存确实覆盖了该窗口的情况下才回退。
    # 内存保留 48 小时，因此 7 天/30 天永远无法覆盖，此时保持 None。
    if uptime_7d is None or uptime_30d is None:
        coverage_hours = AvailabilityService.RETENTION_HOURS
        if coverage_hours >= 24 * 7 and uptime_7d is None:
            uptime_7d = stats.uptime_7d
        if coverage_hours >= 24 * 30 and uptime_30d is None:
            uptime_30d = stats.uptime_30d

    return {
        "node_name": name,
        "uptime_24h": stats.uptime_24h,
        "uptime_7d": uptime_7d,
        "uptime_30d": uptime_30d,
        "long_range_source": long_range_source,
        "coverage_7d": agg_7d.get(name),
        "coverage_30d": agg_30d.get(name),
        "long_range_note": (
            None if long_range_source == "aggregates"
            else "长周期可用性暂无数据：需先执行聚合任务将样本汇总到 aggregates 表"
        ),
        "total_checks_24h": stats.total_checks_24h,
        "success_checks_24h": stats.success_checks_24h,
        "avg_latency_ms": stats.avg_latency_ms,
        "last_check_at": stats.last_check_at.isoformat() + "Z" if stats.last_check_at else None,
        "current_status": stats.current_status,
    }


@router.get("/{name}/availability/trend")
async def get_node_availability_trend(
    name: str,
    hours: int = Query(24, ge=1, le=168, description="查询小时数 (1-168)"),
):
    """
    获取节点可用性趋势数据
    
    返回每小时的可用性状态，用于绘制趋势图。
    """
    # 从数据库查找节点
    repo = _get_db_repository()
    node = None
    if repo:
        try:
            node = await repo.get_node_by_name(name)
        except Exception as e:
            logger.error(f"从数据库获取单个节点失败: {e}")
    
    if not node:
        raise HTTPException(
            status_code=404,
            detail={"code": "E003", "message": f"Node not found: {name}"},
        )
    
    service = get_availability_service()
    trend = service.get_hourly_trend(name, hours)
    
    return {
        "node_name": name,
        "hours": hours,
        "data": [
            {
                "hour": item.hour.isoformat() + "Z",
                "total": item.total,
                "success": item.success,
                "availability": round(item.availability, 2),
                "status": item.status,
            }
            for item in trend
        ],
    }


@router.get("/{name}/latency/aggregates")
async def get_node_latency_aggregates(name: str, hours: int = Query(24, ge=1, le=720)):
    repo = _get_db_repository()
    if repo is None:
        raise HTTPException(status_code=503, detail="数据库不可用")
    try:
        if not await repo.get_node_by_name(name):
            raise HTTPException(status_code=404, detail="节点不存在")
        return {"node_name": name, "window_minutes": 5, "data": await repo.get_latency_aggregates(name, hours)}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=503, detail="读取延迟统计失败") from exc
