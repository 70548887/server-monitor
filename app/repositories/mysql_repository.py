"""
MySQL 异步存储仓库（含自动重连机制）

功能说明：
    - 提供节点、心跳样本的 CRUD 操作
    - 支持数据库连接失败时自动重试
    - 所有操作都包含完善的异常处理和日志记录

使用方法：
    from app.repositories.mysql_repository import get_mysql_repository
    
    repo = get_mysql_repository()
    nodes = await repo.get_all_nodes()

错误处理策略：
    - 连接错误：自动重试（默认3次）
    - 查询错误：记录日志并抛出异常
    - 批量操作：部分失败不影响已成功的操作
"""

import logging
from datetime import datetime, timedelta
from typing import Optional, List

from sqlalchemy import select, delete, update, func, and_, text, case, cast, Integer
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.exc import SQLAlchemyError

from ..models.base import (
    get_session_context, 
    get_session_with_retry,
    init_db, 
    get_engine,
    is_connection_error,
)
from ..models.node import NodeModel
from ..models.sample import SampleModel, AggregateModel
from ..utils.timeutil import utcnow

logger = logging.getLogger(__name__)


class MySQLRepository:
    """
    异步 MySQL 持久化仓库（支持自动重连）
    
    提供节点、心跳样本的完整 CRUD 操作，包含：
    - 节点管理：创建、更新、删除、查询
    - 样本管理：保存、查询、清理
    - 状态更新：实时更新节点状态
    """
    
    def __init__(self, use_retry: bool = True):
        """
        初始化仓库
        
        Args:
            use_retry: 是否启用自动重连机制（默认启用）
                       启用后连接失败会自动重试 3 次
        """
        self._use_retry = use_retry
        logger.debug(f"MySQLRepository initialized: use_retry={use_retry}")
    
    def _get_session_manager(self):
        """
        获取 session 上下文管理器
        
        根据配置返回不同的 session 管理器：
        - use_retry=True: 返回带重试机制的 session
        - use_retry=False: 返回普通 session
        """
        if self._use_retry:
            return get_session_with_retry()
        return get_session_context()
    
    async def save_sample(self, sample_data: dict) -> int:
        """
        保存单个心跳样本
        
        Args:
            sample_data: 样本数据，必须包含 node_name, url, latency_ms
        
        Returns:
            新创建的样本 ID
        
        Raises:
            SQLAlchemyError: 数据库操作失败
        """
        try:
            async with self._get_session_manager() as session:
                sample = SampleModel(
                    node_name=sample_data["node_name"],
                    url=sample_data["url"],
                    timestamp=sample_data.get("timestamp", utcnow()),
                    status_code=sample_data.get("status_code", 0),
                    latency_ms=sample_data["latency_ms"],
                    success=sample_data.get("success", False),
                    error_type=sample_data.get("error_type"),
                    error_message=sample_data.get("error_message"),
                    response_size=sample_data.get("response_size"),
                    dns_ms=sample_data.get("dns_ms"),
                    tcp_ms=sample_data.get("tcp_ms"),
                    tls_ms=sample_data.get("tls_ms"),
                    ttfb_ms=sample_data.get("ttfb_ms"),
                )
                session.add(sample)
                await session.flush()
                logger.debug(f"Sample saved: node={sample_data['node_name']}, id={sample.id}")
                return sample.id
        except SQLAlchemyError as e:
            logger.error(
                f"Failed to save sample: node={sample_data.get('node_name')}, "
                f"error_type={type(e).__name__}, error={e}"
            )
            raise
        except Exception as e:
            logger.error(
                f"Unexpected error saving sample: node={sample_data.get('node_name')}, "
                f"error_type={type(e).__name__}, error={e}"
            )
            raise
    
    async def get_samples(
        self,
        node_name: str,
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None,
        limit: int = 100,
    ) -> list[dict]:
        """
        获取节点的样本列表
        
        Args:
            node_name: 节点名称
            start_time: 开始时间（可选）
            end_time: 结束时间（可选）
            limit: 最大返回数量
        
        Returns:
            样本字典列表，按时间倒序排列
        """
        try:
            async with self._get_session_manager() as session:
                stmt = select(SampleModel).where(SampleModel.node_name == node_name)
                
                if start_time:
                    stmt = stmt.where(SampleModel.timestamp >= start_time)
                if end_time:
                    stmt = stmt.where(SampleModel.timestamp <= end_time)
                
                stmt = stmt.order_by(SampleModel.timestamp.desc()).limit(limit)
                result = await session.execute(stmt)
                samples = result.scalars().all()
                
                logger.debug(f"Retrieved {len(samples)} samples for node={node_name}")
                return [s.to_dict() for s in samples]
        except SQLAlchemyError as e:
            logger.error(
                f"Failed to get samples: node={node_name}, "
                f"error_type={type(e).__name__}, error={e}"
            )
            return []  # 返回空列表而不是抛出异常，避免影响上层业务
        except Exception as e:
            logger.error(
                f"Unexpected error getting samples: node={node_name}, "
                f"error_type={type(e).__name__}, error={e}"
            )
            return []
    
    async def update_node_status(
        self,
        node_name: str,
        status_code: int,
        latency_ms: float,
        error: Optional[str],
        success: bool,
    ):
        """
        更新节点状态（心跳检测后调用）
        
        Args:
            node_name: 节点名称
            status_code: HTTP 状态码
            latency_ms: 响应延迟（毫秒）
            error: 错误信息（成功时为 None）
            success: 是否成功
        
        注意：
            - 成功时重置连续失败计数
            - 失败时增加连续失败计数
        """
        try:
            async with self._get_session_manager() as session:
                stmt = select(NodeModel).where(
                    NodeModel.name == node_name,
                    NodeModel.deleted_at.is_(None),
                )
                result = await session.execute(stmt)
                node = result.scalar_one_or_none()
                
                if node:
                    node.last_status_code = status_code
                    node.last_latency_ms = latency_ms
                    node.last_error = error
                    node.last_check_at = utcnow()
                    
                    if success:
                        node.consecutive_failures = 0
                    else:
                        node.consecutive_failures = (node.consecutive_failures or 0) + 1
                    
                    logger.debug(
                        f"Node status updated: node={node_name}, status={status_code}, "
                        f"latency={latency_ms:.2f}ms, success={success}"
                    )
                else:
                    logger.warning(f"Node not found for status update: node={node_name}")
        except SQLAlchemyError as e:
            logger.error(
                f"Failed to update node status: node={node_name}, "
                f"error_type={type(e).__name__}, error={e}"
            )
        except Exception as e:
            logger.error(
                f"Unexpected error updating node status: node={node_name}, "
                f"error_type={type(e).__name__}, error={e}"
            )
    
    async def cleanup_old_samples(self, days: int = 30) -> int:
        """
        清理过期样本
        
        Args:
            days: 保留天数，超过此时间的样本将被删除
        
        Returns:
            删除的样本数量
        """
        cutoff = utcnow() - timedelta(days=days)
        
        try:
            async with self._get_session_manager() as session:
                stmt = delete(SampleModel).where(SampleModel.timestamp < cutoff)
                result = await session.execute(stmt)
                count = result.rowcount
                logger.info(f"Cleaned up {count} old samples (older than {days} days)")
                return count
        except SQLAlchemyError as e:
            logger.error(
                f"Failed to cleanup old samples: days={days}, "
                f"error_type={type(e).__name__}, error={e}"
            )
            return 0
        except Exception as e:
            logger.error(
                f"Unexpected error cleaning up samples: days={days}, "
                f"error_type={type(e).__name__}, error={e}"
            )
            return 0
    
    # ============= 聚合快照（长期可用性）=============

    async def rollup_samples_to_aggregates(
        self,
        window_minutes: int = 5,
        hours: int = 6,
        lookback_minutes: Optional[int] = None,
    ) -> dict:
        """
        把原始样本汇总为窗口聚合快照

        为什么需要它：内存可用性服务只保留 48 小时记录，因此
        uptime_7d / uptime_30d 此前实际拿到的是"残缺窗口"——只有最近
        48 小时的数据参与计算，却按 7 天/30 天展示，属于静默失真。
        聚合表按 5 分钟窗口长期保留，让长周期统计有真实数据来源。

        实现上把分组与计数交给 MySQL（GROUP BY），只把每个窗口的
        一行结果写回，避免把几十万条样本拉进 Python 内存。

        Args:
            window_minutes: 窗口大小（分钟）
            hours: 回溯多少小时的数据参与汇总
            lookback_minutes: 直接指定回溯分钟数（优先于 hours）

        Returns:
            {"windows": 处理的窗口数, "written": 写入行数, "samples": 覆盖样本数}
        """
        window_seconds = window_minutes * 60
        lookback = lookback_minutes if lookback_minutes is not None else hours * 60

        # 只汇总"已经结束"的窗口，避免把当前不完整的窗口写进去后又被覆盖
        now = utcnow()
        end_time = now - timedelta(seconds=window_seconds)
        start_time = end_time - timedelta(minutes=lookback)

        try:
            async with self._get_session_manager() as session:
                # SQL 侧按节点 + 窗口分组聚合
                # 注意：用 UNIX_TIMESTAMP 的整除来做窗口对齐，
                # 比字符串处理时间更可靠，也能命中 timestamp 索引。
                bucket = func.floor(
                    func.unix_timestamp(SampleModel.timestamp) / window_seconds
                )

                stmt = (
                    select(
                        SampleModel.node_name.label("node_name"),
                        bucket.label("bucket"),
                        func.count().label("total_count"),
                        # 成功计数：把布尔列转成整数再求和。
                        # 不用 func.case(...)：case 是顶层构造器，
                        # 套进 func 会因 else_ 参数报 TypeError。
                        func.sum(
                            cast(SampleModel.success, Integer)
                        ).label("success_count"),
                        func.min(SampleModel.latency_ms).label("latency_min"),
                        func.max(SampleModel.latency_ms).label("latency_max"),
                        func.avg(SampleModel.latency_ms).label("latency_avg"),
                    )
                    .where(
                        and_(
                            SampleModel.timestamp >= start_time,
                            SampleModel.timestamp < end_time,
                        )
                    )
                    .group_by(SampleModel.node_name, bucket)
                )

                result = await session.execute(stmt)
                rows = result.all()

                if not rows:
                    return {"windows": 0, "written": 0, "samples": 0}

                # 已存在的窗口先删除，保证重跑幂等
                window_starts = [
                    datetime.utcfromtimestamp(int(r.bucket) * window_seconds)
                    for r in rows
                ]
                node_names = {r.node_name for r in rows}

                await session.execute(
                    delete(AggregateModel).where(
                        and_(
                            AggregateModel.node_name.in_(node_names),
                            AggregateModel.window_start.in_(window_starts),
                        )
                    )
                )

                total_samples = 0
                for r in rows:
                    window_start = datetime.utcfromtimestamp(
                        int(r.bucket) * window_seconds
                    )
                    total = int(r.total_count or 0)
                    success = int(r.success_count or 0)
                    total_samples += total

                    session.add(AggregateModel(
                        node_name=r.node_name,
                        window_start=window_start,
                        window_end=window_start + timedelta(seconds=window_seconds),
                        window_minutes=window_minutes,
                        total_count=total,
                        success_count=success,
                        error_count=total - success,
                        error_rate=round((total - success) / total, 4) if total else 0.0,
                        latency_min_ms=r.latency_min,
                        latency_max_ms=r.latency_max,
                        latency_avg_ms=round(r.latency_avg, 2) if r.latency_avg else None,
                        # 窗口内的可用性比例；p50/p95/p99 需要样本级数据，
                        # 这里不做近似，避免给出看起来精确却无依据的数字
                        availability=round(success / total, 4) if total else 1.0,
                    ))

                await session.commit()

                logger.info(
                    f"聚合完成: {len(rows)} 个窗口, 覆盖 {total_samples} 条样本, "
                    f"窗口大小 {window_minutes} 分钟"
                )
                return {
                    "windows": len(rows),
                    "written": len(rows),
                    "samples": total_samples,
                }

        except SQLAlchemyError as e:
            logger.error(
                f"聚合样本失败: error_type={type(e).__name__}, error={e}"
            )
            return {"windows": 0, "written": 0, "samples": 0, "error": str(e)}
        except Exception as e:
            logger.error(
                f"聚合样本异常: error_type={type(e).__name__}, error={e}"
            )
            return {"windows": 0, "written": 0, "samples": 0, "error": str(e)}

    async def get_aggregated_uptime(
        self,
        node_name: Optional[str] = None,
        days: int = 30,
    ) -> dict:
        """
        从聚合表读取长周期可用性

        Returns:
            {node_name: {"uptime": 百分比, "total": 样本数, "success": 成功数}}
            传入 node_name 时只返回该节点。
        """
        cutoff = utcnow() - timedelta(days=days)

        try:
            async with self._get_session_manager() as session:
                stmt = (
                    select(
                        AggregateModel.node_name,
                        func.sum(AggregateModel.total_count).label("total"),
                        func.sum(AggregateModel.success_count).label("success"),
                    )
                    .where(AggregateModel.window_start >= cutoff)
                    .group_by(AggregateModel.node_name)
                )
                if node_name:
                    stmt = stmt.where(AggregateModel.node_name == node_name)

                result = await session.execute(stmt)
                rows = result.all()

                out = {}
                for r in rows:
                    total = int(r.total or 0)
                    success = int(r.success or 0)
                    out[r.node_name] = {
                        "uptime": round((success / total) * 100, 3) if total else None,
                        "total_checks": total,
                        "success_checks": success,
                    }
                return out

        except SQLAlchemyError as e:
            logger.error(
                f"读取聚合可用性失败: node={node_name}, days={days}, error={e}"
            )
            return {}
        except Exception as e:
            logger.error(f"读取聚合可用性异常: error={e}")
            return {}

    async def cleanup_old_aggregates(self, days: int = 90) -> int:
        """清理过期聚合快照"""
        cutoff = utcnow() - timedelta(days=days)
        try:
            async with self._get_session_manager() as session:
                result = await session.execute(
                    delete(AggregateModel).where(AggregateModel.window_start < cutoff)
                )
                await session.commit()
                count = result.rowcount
                logger.info(f"Cleaned up {count} old aggregates (older than {days} days)")
                return count
        except SQLAlchemyError as e:
            logger.error(f"Failed to cleanup old aggregates: {e}")
            return 0
        except Exception as e:
            logger.error(f"Unexpected error cleaning up aggregates: {e}")
            return 0

    # ============= 节点 CRUD 操作 =============    
    async def get_all_nodes(self, include_deleted: bool = False) -> List[dict]:
        """
        获取所有节点
        
        Args:
            include_deleted: 是否包含已删除的节点
        
        Returns:
            节点字典列表
        """
        try:
            async with self._get_session_manager() as session:
                stmt = select(NodeModel)
                if not include_deleted:
                    stmt = stmt.where(NodeModel.deleted_at.is_(None))
                result = await session.execute(stmt)
                nodes = result.scalars().all()
                logger.debug(f"Retrieved {len(nodes)} nodes (include_deleted={include_deleted})")
                return [n.to_dict() for n in nodes]
        except SQLAlchemyError as e:
            logger.error(
                f"Failed to get all nodes: include_deleted={include_deleted}, "
                f"error_type={type(e).__name__}, error={e}"
            )
            return []  # 返回空列表，避免影响上层逻辑
        except Exception as e:
            logger.error(
                f"Unexpected error getting nodes: "
                f"error_type={type(e).__name__}, error={e}"
            )
            return []
    
    async def get_node_by_name(self, name: str) -> Optional[dict]:
        """
        根据名称获取节点
        
        Args:
            name: 节点名称
        
        Returns:
            节点字典，不存在返回 None
        """
        try:
            async with self._get_session_manager() as session:
                stmt = select(NodeModel).where(
                    NodeModel.name == name,
                    NodeModel.deleted_at.is_(None),
                )
                result = await session.execute(stmt)
                node = result.scalar_one_or_none()
                if node:
                    logger.debug(f"Node found: name={name}")
                else:
                    logger.debug(f"Node not found: name={name}")
                return node.to_dict() if node else None
        except SQLAlchemyError as e:
            logger.error(
                f"Failed to get node by name: name={name}, "
                f"error_type={type(e).__name__}, error={e}"
            )
            return None
        except Exception as e:
            logger.error(
                f"Unexpected error getting node: name={name}, "
                f"error_type={type(e).__name__}, error={e}"
            )
            return None
    
    async def create_node(self, node_data: dict) -> int:
        """
        创建新节点
        
        Args:
            node_data: 节点数据，必须包含 name 和 url
        
        Returns:
            新创建的节点 ID
        
        Raises:
            SQLAlchemyError: 数据库操作失败（如名称重复）
        """
        try:
            async with self._get_session_manager() as session:
                node = NodeModel(
                    name=node_data["name"],
                    url=node_data["url"],
                    region=node_data.get("region", "default"),
                    tags=node_data.get("tags", []),
                    timeout_ms=node_data.get("timeout_ms", 2000),
                    enabled=node_data.get("enabled", True),
                    expect_status=node_data.get("expect", {}).get("status", 200),
                    expect_body_contains=node_data.get("expect", {}).get("body_contains"),
                )
                session.add(node)
                await session.flush()
                logger.info(f"节点已创建到数据库: 名称={node_data['name']}, id={node.id}")
                return node.id
        except SQLAlchemyError as e:
            logger.error(
                f"Failed to create node: name={node_data.get('name')}, "
                f"error_type={type(e).__name__}, error={e}"
            )
            raise
        except Exception as e:
            logger.error(
                f"Unexpected error creating node: name={node_data.get('name')}, "
                f"error_type={type(e).__name__}, error={e}"
            )
            raise
    
    async def update_node(self, name: str, update_data: dict) -> bool:
        """
        更新节点配置
        
        Args:
            name: 节点名称
            update_data: 要更新的字段
        
        Returns:
            更新是否成功（节点不存在返回 False）
        """
        try:
            async with self._get_session_manager() as session:
                stmt = select(NodeModel).where(
                    NodeModel.name == name,
                    NodeModel.deleted_at.is_(None),
                )
                result = await session.execute(stmt)
                node = result.scalar_one_or_none()
                
                if not node:
                    logger.warning(f"Node not found for update: name={name}")
                    return False
                
                # 更新各字段（仅更新提供的字段）
                if "url" in update_data:
                    node.url = update_data["url"]
                if "region" in update_data:
                    node.region = update_data["region"]
                if "tags" in update_data:
                    node.tags = update_data["tags"]
                if "timeout_ms" in update_data:
                    node.timeout_ms = update_data["timeout_ms"]
                if "enabled" in update_data:
                    node.enabled = update_data["enabled"]
                if "expect" in update_data:
                    node.expect_status = update_data["expect"].get("status", 200)
                    node.expect_body_contains = update_data["expect"].get("body_contains")
                
                logger.info(f"Node updated in DB: name={name}, fields={list(update_data.keys())}")
                return True
        except SQLAlchemyError as e:
            logger.error(
                f"Failed to update node: name={name}, "
                f"error_type={type(e).__name__}, error={e}"
            )
            return False
        except Exception as e:
            logger.error(
                f"Unexpected error updating node: name={name}, "
                f"error_type={type(e).__name__}, error={e}"
            )
            return False
    
    async def delete_node(self, name: str, hard: bool = False) -> bool:
        """
        删除节点
        
        Args:
            name: 节点名称
            hard: 是否硬删除（永久删除，默认为软删除）
        
        Returns:
            删除是否成功
        
        注意：
            - 软删除：设置 deleted_at 并禁用节点，数据保留
            - 硬删除：永久删除记录，不可恢复
        """
        try:
            async with self._get_session_manager() as session:
                stmt = select(NodeModel).where(
                    NodeModel.name == name,
                    NodeModel.deleted_at.is_(None),
                )
                result = await session.execute(stmt)
                node = result.scalar_one_or_none()
                
                if not node:
                    logger.warning(f"Node not found for deletion: name={name}")
                    return False
                
                if hard:
                    await session.delete(node)
                    logger.info(f"Node hard deleted from DB: name={name}")
                else:
                    node.deleted_at = utcnow()
                    node.enabled = False
                    logger.info(f"Node soft deleted in DB: name={name}")
                
                return True
        except SQLAlchemyError as e:
            logger.error(
                f"Failed to delete node: name={name}, hard={hard}, "
                f"error_type={type(e).__name__}, error={e}"
            )
            return False
        except Exception as e:
            logger.error(
                f"Unexpected error deleting node: name={name}, "
                f"error_type={type(e).__name__}, error={e}"
            )
            return False
    
    async def upsert_node(self, node_data: dict) -> int:
        """
        插入或更新节点（用于同步）
        
        Args:
            node_data: 节点数据，必须包含 name 和 url
        
        Returns:
            节点 ID
        
        注意：
            - 节点存在则更新配置
            - 节点不存在则创建新记录
            - 已软删除的节点会被恢复
        """
        try:
            async with self._get_session_manager() as session:
                stmt = select(NodeModel).where(NodeModel.name == node_data["name"])
                result = await session.execute(stmt)
                node = result.scalar_one_or_none()
                
                if node:
                    # 更新现有节点
                    node.url = node_data["url"]
                    node.region = node_data.get("region", "default")
                    node.tags = node_data.get("tags", [])
                    node.timeout_ms = node_data.get("timeout_ms", 2000)
                    node.enabled = node_data.get("enabled", True)
                    node.expect_status = node_data.get("expect", {}).get("status", 200)
                    node.expect_body_contains = node_data.get("expect", {}).get("body_contains")
                    node.deleted_at = None  # 恢复软删除的节点
                    await session.flush()
                    logger.debug(f"Node upserted (updated): name={node_data['name']}, id={node.id}")
                    return node.id
                else:
                    # 创建新节点
                    new_node = NodeModel(
                        name=node_data["name"],
                        url=node_data["url"],
                        region=node_data.get("region", "default"),
                        tags=node_data.get("tags", []),
                        timeout_ms=node_data.get("timeout_ms", 2000),
                        enabled=node_data.get("enabled", True),
                        expect_status=node_data.get("expect", {}).get("status", 200),
                        expect_body_contains=node_data.get("expect", {}).get("body_contains"),
                    )
                    session.add(new_node)
                    await session.flush()
                    logger.debug(f"节点已插入 (新建): 名称={node_data['name']}, id={new_node.id}")
                    return new_node.id
        except SQLAlchemyError as e:
            logger.error(
                f"Failed to upsert node: name={node_data.get('name')}, "
                f"error_type={type(e).__name__}, error={e}"
            )
            raise
        except Exception as e:
            logger.error(
                f"Unexpected error upserting node: name={node_data.get('name')}, "
                f"error_type={type(e).__name__}, error={e}"
            )
            raise
    
    async def save_samples_batch(self, samples: List[dict]) -> int:
        """
        批量保存心跳样本
        
        Args:
            samples: 样本字典列表，每个必须包含 node_name, url, latency_ms
        
        Returns:
            成功保存的样本数量
        
        注意：
            - 批量插入效率更高
            - 如果部分失败不影响已成功的记录
        """
        if not samples:
            return 0
        
        try:
            async with self._get_session_manager() as session:
                sample_objects = [
                    SampleModel(
                        node_name=s["node_name"],
                        url=s["url"],
                        timestamp=s.get("timestamp", utcnow()),
                        status_code=s.get("status_code", 0),
                        latency_ms=s["latency_ms"],
                        success=s.get("success", False),
                        error_type=s.get("error_type"),
                        error_message=s.get("error_message"),
                        response_size=s.get("response_size"),
                    )
                    for s in samples
                ]
                session.add_all(sample_objects)
                await session.flush()
                logger.debug(f"Batch saved {len(sample_objects)} samples")
                return len(sample_objects)
        except SQLAlchemyError as e:
            logger.error(
                f"Failed to batch save samples: count={len(samples)}, "
                f"error_type={type(e).__name__}, error={e}"
            )
            return 0
        except Exception as e:
            logger.error(
                f"Unexpected error batch saving samples: count={len(samples)}, "
                f"error_type={type(e).__name__}, error={e}"
            )
            return 0
    
    async def get_recent_samples(self, hours: int = 24) -> List[dict]:
        """
        获取最近N小时的所有样本（用于启动时加载）
        
        Args:
            hours: 加载时间范围（小时）
        
        Returns:
            样本字典列表，按时间升序排列
        
        注意：
            - 只返回当前数据库中存在的节点的样本
            - 已删除节点的样本不会返回
        """
        cutoff = utcnow() - timedelta(hours=hours)
        
        try:
            async with self._get_session_manager() as session:
                # 获取当前存在的节点名称列表
                existing_nodes_stmt = select(NodeModel.name).where(NodeModel.deleted_at.is_(None))
                
                # 只查询这些节点的样本
                stmt = select(SampleModel).where(
                    SampleModel.timestamp >= cutoff,
                    SampleModel.node_name.in_(existing_nodes_stmt)
                ).order_by(SampleModel.timestamp.asc())
                
                result = await session.execute(stmt)
                samples = result.scalars().all()
                
                logger.info(f"已加载过去 {hours} 小时的 {len(samples)} 条样本")
                
                return [
                    {
                        "node_name": s.node_name,
                        "timestamp": s.timestamp,
                        "success": s.success,
                        "latency_ms": s.latency_ms,
                        "status_code": s.status_code,
                        "error_type": s.error_type,
                    }
                    for s in samples
                ]
        except SQLAlchemyError as e:
            logger.error(
                f"Failed to get recent samples: hours={hours}, "
                f"error_type={type(e).__name__}, error={e}"
            )
            return []
        except Exception as e:
            logger.error(
                f"Unexpected error getting recent samples: hours={hours}, "
                f"error_type={type(e).__name__}, error={e}"
            )
            return []


# 全局实例
_mysql_repo: Optional[MySQLRepository] = None


def get_mysql_repository() -> MySQLRepository:
    """
    获取 MySQL 仓库单例
    
    Returns:
        MySQLRepository 实例
    
    示例：
        repo = get_mysql_repository()
        nodes = await repo.get_all_nodes()
    """
    global _mysql_repo
    if _mysql_repo is None:
        _mysql_repo = MySQLRepository()
        logger.debug("已创建 MySQLRepository 实例")
    return _mysql_repo
