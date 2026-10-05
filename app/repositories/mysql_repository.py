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

from sqlalchemy import select, delete, update
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
                    timestamp=sample_data.get("timestamp", datetime.utcnow()),
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
                    node.last_check_at = datetime.utcnow()
                    
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
        cutoff = datetime.utcnow() - timedelta(days=days)
        
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
                    node.deleted_at = datetime.utcnow()
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
                        timestamp=s.get("timestamp", datetime.utcnow()),
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
        cutoff = datetime.utcnow() - timedelta(hours=hours)
        
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
