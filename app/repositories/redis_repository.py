"""
Redis 异步存储仓库

功能说明：
    - 提供节点状态缓存、错误计数、告警抑制等功能
    - 支持 Redis 不可用时的降级处理
    - 所有操作都包含完善的异常处理和日志记录

使用方法：
    from app.repositories.redis_repository import get_redis_repository
    
    repo = get_redis_repository()
    ok, latency = await repo.ping()

错误处理策略：
    - 连接错误：记录日志并返回默认值
    - Redis 不可用：不影响主流程，降级为内存模式
    - 网络超时：记录警告并继续执行
"""

import json
import logging
import time
from datetime import datetime
from typing import Optional, Tuple

import redis.asyncio as aioredis
from redis.backoff import NoBackoff
from redis.exceptions import RedisError, ConnectionError as RedisConnectionError
from redis.retry import Retry

from ..services.config import get_config

logger = logging.getLogger(__name__)


class RedisRepository:
    """
    异步 Redis 缓存仓库
    
    提供以下功能：
    - 节点状态缓存：快速获取节点最新状态
    - 错误计数：滑动窗口内的错误次数统计
    - 告警抑制：避免重复发送同类告警
    
    注意：
        - Redis 不可用时不影响主流程
        - 所有方法都有异常处理，失败时返回默认值
    """
    
    def __init__(self):
        """
        初始化 Redis 仓库
        
        连接不会立即建立，而是在首次使用时懒加载
        """
        self._client: Optional[aioredis.Redis] = None
        self._prefix = "srvmon:"  # 键前缀，用于命名空间隔离
        self._connected = False  # 连接状态标记
        logger.debug("RedisRepository initialized")
    
    async def _get_client(self) -> aioredis.Redis:
        """
        获取异步 Redis 客户端（懒加载）
        
        从配置中读取连接参数，首次调用时创建连接
        
        Returns:
            Redis 客户端实例
        
        Raises:
            RedisError: 连接失败
        """
        if self._client is None:
            try:
                config = get_config()
                self._client = aioredis.Redis(
                    host=config.database.redis_host,
                    port=config.database.redis_port,
                    db=config.database.redis_db,
                    password=config.database.redis_password or None,
                    decode_responses=True,
                    # 超时与重试策略（重要）：
                    # Redis 在本项目中是可选组件，不可用时降级到内存实现。
                    # redis-py 默认带重试与指数退避，实测 Redis 宕机时单次
                    # incr 会阻塞约 25 秒 —— 意味着每个请求都卡住、
                    # 限流降级前先被拖死、k8s 探针超时导致 Pod 反复重启。
                    # 故关闭重试并压低超时，让降级在亚秒级完成。
                    socket_connect_timeout=1,
                    socket_timeout=1,
                    retry=Retry(NoBackoff(), 0),
                    retry_on_timeout=False,
                )
                self._prefix = config.database.redis_prefix
                logger.debug(
                    f"Redis client created: host={config.database.redis_host}, "
                    f"port={config.database.redis_port}, db={config.database.redis_db}"
                )
            except Exception as e:
                logger.error(
                    f"Failed to create Redis client: "
                    f"error_type={type(e).__name__}, error={e}"
                )
                raise
        return self._client
    
    async def ping(self) -> Tuple[bool, float]:
        """
        异步 Ping Redis 检查连接状态
        
        Returns:
            (is_connected, latency_ms) 元组
            - is_connected: 是否连接成功
            - latency_ms: 响应延迟（毫秒）
        """
        start = time.perf_counter()
        try:
            client = await self._get_client()
            await client.ping()
            latency = (time.perf_counter() - start) * 1000
            self._connected = True
            logger.debug(f"Redis ping successful: latency={latency:.2f}ms")
            return True, latency
        except RedisConnectionError as e:
            latency = (time.perf_counter() - start) * 1000
            self._connected = False
            logger.error(
                f"Redis connection error: "
                f"error_type={type(e).__name__}, error={e}, latency={latency:.2f}ms"
            )
            return False, latency
        except RedisError as e:
            latency = (time.perf_counter() - start) * 1000
            self._connected = False
            logger.error(
                f"Redis ping failed: "
                f"error_type={type(e).__name__}, error={e}, latency={latency:.2f}ms"
            )
            return False, latency
        except Exception as e:
            latency = (time.perf_counter() - start) * 1000
            self._connected = False
            logger.error(
                f"Unexpected error pinging Redis: "
                f"error_type={type(e).__name__}, error={e}"
            )
            return False, latency
    
    async def cache_node_status(self, node_name: str, status: dict, ttl: int = 300):
        """
        缓存节点状态
        
        Args:
            node_name: 节点名称
            status: 状态字典（将被 JSON 序列化）
            ttl: 过期时间（秒），默认 5 分钟
        
        注意：
            - 操作失败不会抛出异常，仅记录日志
            - Redis 不可用时降级为内存缓存
        """
        try:
            client = await self._get_client()
            key = f"{self._prefix}node:{node_name}:status"
            await client.setex(key, ttl, json.dumps(status, ensure_ascii=False))
            logger.debug(f"Node status cached: node={node_name}, ttl={ttl}s")
        except RedisConnectionError as e:
            logger.warning(
                f"Redis connection error while caching status: node={node_name}, "
                f"error_type={type(e).__name__}, error={e}"
            )
        except RedisError as e:
            logger.error(
                f"Failed to cache node status: node={node_name}, "
                f"error_type={type(e).__name__}, error={e}"
            )
        except Exception as e:
            logger.error(
                f"Unexpected error caching node status: node={node_name}, "
                f"error_type={type(e).__name__}, error={e}"
            )
    
    async def get_node_status(self, node_name: str) -> Optional[dict]:
        """
        获取缓存的节点状态
        
        Args:
            node_name: 节点名称
        
        Returns:
            状态字典，缓存不存在或读取失败返回 None
        """
        try:
            client = await self._get_client()
            key = f"{self._prefix}node:{node_name}:status"
            data = await client.get(key)
            if data:
                logger.debug(f"Node status retrieved from cache: node={node_name}")
                return json.loads(data)
            logger.debug(f"Node status not in cache: node={node_name}")
            return None
        except RedisConnectionError as e:
            logger.warning(
                f"Redis connection error while getting status: node={node_name}, "
                f"error_type={type(e).__name__}, error={e}"
            )
            return None
        except json.JSONDecodeError as e:
            logger.error(
                f"Invalid JSON in cached status: node={node_name}, error={e}"
            )
            return None
        except RedisError as e:
            logger.error(
                f"Failed to get node status: node={node_name}, "
                f"error_type={type(e).__name__}, error={e}"
            )
            return None
        except Exception as e:
            logger.error(
                f"Unexpected error getting node status: node={node_name}, "
                f"error_type={type(e).__name__}, error={e}"
            )
            return None
    
    async def rename_node_state(self, old_name: str, new_name: str) -> bool:
        """Atomically move node caches, preserving TTLs and replacing stale targets.

        Missing source keys also clear their destination, so a reused node name
        cannot inherit suppression from a previously deleted node. A Redis
        outage is best-effort and never rolls back the committed database rename.
        """
        if old_name == new_name:
            return True
        try:
            client = await self._get_client()
            key_templates = [
                "alert:suppress:{}:node_down",
                "alert:suppress:{}:node_recovery",
                "alert:suppress:{}:latency_high",
                "node:{}:status",
                "node:{}:errors",
            ]
            keys = [
                f"{self._prefix}{template.format(name)}"
                for template in key_templates
                for name in (old_name, new_name)
            ]
            await client.eval("""
                for i = 1, #KEYS, 2 do
                    if redis.call('EXISTS', KEYS[i]) == 1 then
                        redis.call('RENAME', KEYS[i], KEYS[i + 1])
                    else
                        redis.call('DEL', KEYS[i + 1])
                    end
                end
                return 1
            """, len(keys), *keys)
            return True
        except Exception:
            logger.warning("Redis node-state rename failed; using renamed memory state", exc_info=True)
            return False

    async def rate_limit_increment(self, key: str, window_seconds: int) -> int:
        """限流专用严格接口：原子计数/过期，连接异常交给调用方降级。"""
        client = await self._get_client()
        return int(await client.eval("""
            local count = redis.call('INCR', KEYS[1])
            if redis.call('TTL', KEYS[1]) < 0 then
                redis.call('EXPIRE', KEYS[1], ARGV[1])
            end
            return count
        """, 1, f"{self._prefix}node:{key}:errors", window_seconds))

    def _login_limit_keys(self, prefix: str, ip: str):
        # 保留已有键格式，升级后不会重置尚未过期的失败次数和锁定。
        return (f"{self._prefix}node:{prefix}login_fail:{ip}:errors",
                f"{self._prefix}alert:suppress:{prefix}login_lock:{ip}")

    async def login_failure_increment(self, prefix: str, ip: str, limit: int, seconds: int) -> bool:
        client = await self._get_client()
        count = await client.eval("""
            local count = redis.call('INCR', KEYS[1])
            if redis.call('TTL', KEYS[1]) < 0 then
                redis.call('EXPIRE', KEYS[1], ARGV[2])
            end
            if count >= tonumber(ARGV[1]) then
                redis.call('SETEX', KEYS[2], ARGV[2], '1')
            end
            return count
        """, 2, *self._login_limit_keys(prefix, ip), limit, seconds)
        return int(count) >= limit

    async def login_lock_ttl(self, prefix: str, ip: str) -> int:
        client = await self._get_client()
        return int(await client.ttl(self._login_limit_keys(prefix, ip)[1]))

    async def clear_login_limits(self, prefix: str, ip: str):
        client = await self._get_client()
        await client.delete(*self._login_limit_keys(prefix, ip))

    async def increment_error_count(self, node_name: str, window_seconds: int = 300) -> int:
        """
        增加错误计数（滑动窗口）
        
        Args:
            node_name: 节点名称
            window_seconds: 窗口时间（秒），默认 5 分钟
        
        Returns:
            当前窗口内的错误次数，失败返回 0
        
        注意：
            - 使用 INCR + EXPIRE 实现滑动窗口
            - 第一次计数时设置过期时间
        """
        try:
            client = await self._get_client()
            key = f"{self._prefix}node:{node_name}:errors"
            
            # 使用 INCR + EXPIRE 实现滑动窗口
            count = await client.incr(key)
            if count == 1:
                # 第一次计数时设置过期时间
                await client.expire(key, window_seconds)
            
            logger.debug(
                f"Error count incremented: node={node_name}, "
                f"count={count}, window={window_seconds}s"
            )
            return count
        except RedisConnectionError as e:
            logger.warning(
                f"Redis connection error while incrementing error count: "
                f"node={node_name}, error_type={type(e).__name__}, error={e}"
            )
            return 0
        except RedisError as e:
            logger.error(
                f"Failed to increment error count: node={node_name}, "
                f"error_type={type(e).__name__}, error={e}"
            )
            return 0
        except Exception as e:
            logger.error(
                f"Unexpected error incrementing error count: node={node_name}, "
                f"error_type={type(e).__name__}, error={e}"
            )
            return 0
    
    async def get_error_count(self, node_name: str) -> int:
        """
        获取当前错误计数
        
        Args:
            node_name: 节点名称
        
        Returns:
            当前窗口内的错误次数，失败返回 0
        """
        try:
            client = await self._get_client()
            key = f"{self._prefix}node:{node_name}:errors"
            count = await client.get(key)
            result = int(count) if count else 0
            logger.debug(f"Error count retrieved: node={node_name}, count={result}")
            return result
        except (ValueError, TypeError) as e:
            logger.error(
                f"Invalid error count value: node={node_name}, error={e}"
            )
            return 0
        except RedisConnectionError as e:
            logger.warning(
                f"Redis connection error while getting error count: "
                f"node={node_name}, error_type={type(e).__name__}, error={e}"
            )
            return 0
        except RedisError as e:
            logger.error(
                f"Failed to get error count: node={node_name}, "
                f"error_type={type(e).__name__}, error={e}"
            )
            return 0
        except Exception as e:
            logger.error(
                f"Unexpected error getting error count: node={node_name}, "
                f"error_type={type(e).__name__}, error={e}"
            )
            return 0
    
    async def set_alert_suppression(self, alert_key: str, suppress_seconds: int):
        """
        设置告警抑制
        
        Args:
            alert_key: 告警唯一标识（如 node_name:alert_type）
            suppress_seconds: 抑制时间（秒）
        
        注意：
            - 抑制时间内不会重复发送同类告警
            - 操作失败不抛出异常，避免影响主流程
        """
        try:
            client = await self._get_client()
            key = f"{self._prefix}alert:suppress:{alert_key}"
            await client.setex(key, suppress_seconds, "1")
            logger.debug(
                f"Alert suppression set: key={alert_key}, duration={suppress_seconds}s"
            )
        except RedisConnectionError as e:
            logger.warning(
                f"Redis connection error while setting alert suppression: "
                f"key={alert_key}, error_type={type(e).__name__}, error={e}"
            )
        except RedisError as e:
            logger.error(
                f"Failed to set alert suppression: key={alert_key}, "
                f"error_type={type(e).__name__}, error={e}"
            )
        except Exception as e:
            logger.error(
                f"Unexpected error setting alert suppression: key={alert_key}, "
                f"error_type={type(e).__name__}, error={e}"
            )
    
    async def is_alert_suppressed(self, alert_key: str) -> bool:
        """
        检查告警是否被抑制
        
        Args:
            alert_key: 告警唯一标识
        
        Returns:
            True 表示告警被抑制，不应发送
            False 表示可以发送告警（或 Redis 不可用时的默认值）
        """
        try:
            client = await self._get_client()
            key = f"{self._prefix}alert:suppress:{alert_key}"
            result = await client.exists(key) > 0
            logger.debug(f"Alert suppression check: key={alert_key}, suppressed={result}")
            return result
        except RedisConnectionError as e:
            # Redis 不可用时不抑制告警，确保重要告警能发送
            logger.warning(
                f"Redis connection error while checking alert suppression: "
                f"key={alert_key}, error_type={type(e).__name__}, error={e}"
            )
            return False
        except RedisError as e:
            logger.error(
                f"Failed to check alert suppression: key={alert_key}, "
                f"error_type={type(e).__name__}, error={e}"
            )
            return False
        except Exception as e:
            logger.error(
                f"Unexpected error checking alert suppression: key={alert_key}, "
                f"error_type={type(e).__name__}, error={e}"
            )
            return False
    
    async def clear_alert_suppression(self, alert_key: str) -> bool:
        """
        清除告警抑制

        用于节点恢复或人工重置，使后续告警不再被抑制。

        Args:
            alert_key: 告警唯一标识

        Returns:
            True 表示键被删除，False 表示键不存在或失败
        """
        try:
            client = await self._get_client()
            key = f"{self._prefix}alert:suppress:{alert_key}"
            deleted = await client.delete(key)
            logger.debug(f"Alert suppression cleared: key={alert_key}, deleted={deleted}")
            return deleted > 0
        except RedisConnectionError as e:
            logger.warning(
                f"Redis connection error while clearing alert suppression: "
                f"key={alert_key}, error_type={type(e).__name__}, error={e}"
            )
            return False
        except RedisError as e:
            logger.error(
                f"Failed to clear alert suppression: key={alert_key}, "
                f"error_type={type(e).__name__}, error={e}"
            )
            return False
        except Exception as e:
            logger.error(
                f"Unexpected error clearing alert suppression: key={alert_key}, "
                f"error_type={type(e).__name__}, error={e}"
            )
            return False

    async def hset(self, key: str, mapping: dict):
        """
        设置 Hash 字段
        
        Args:
            key: Hash 键名
            mapping: 字段与值的映射
        
        Raises:
            RedisError: 操作失败
        """
        try:
            client = await self._get_client()
            # 逐个设置字段，兼容旧版本 Redis
            for field, value in mapping.items():
                await client.hset(key, field, value)
            logger.debug(f"Hash fields set: key={key}, fields={list(mapping.keys())}")
        except RedisConnectionError as e:
            logger.error(
                f"Redis connection error while setting hash: key={key}, "
                f"error_type={type(e).__name__}, error={e}"
            )
            raise
        except RedisError as e:
            logger.error(
                f"Failed to set hash: key={key}, "
                f"error_type={type(e).__name__}, error={e}"
            )
            raise
        except Exception as e:
            logger.error(
                f"Unexpected error setting hash: key={key}, "
                f"error_type={type(e).__name__}, error={e}"
            )
            raise
    
    async def hgetall(self, key: str) -> Optional[dict]:
        """
        获取 Hash 所有字段
        
        Args:
            key: Hash 键名
        
        Returns:
            字段与值的字典，键不存在或失败返回 None
        """
        try:
            client = await self._get_client()
            data = await client.hgetall(key)
            if data:
                logger.debug(f"Hash retrieved: key={key}, fields={list(data.keys())}")
            return data if data else None
        except RedisConnectionError as e:
            logger.warning(
                f"Redis connection error while getting hash: key={key}, "
                f"error_type={type(e).__name__}, error={e}"
            )
            return None
        except RedisError as e:
            logger.error(
                f"Failed to get hash: key={key}, "
                f"error_type={type(e).__name__}, error={e}"
            )
            return None
        except Exception as e:
            logger.error(
                f"Unexpected error getting hash: key={key}, "
                f"error_type={type(e).__name__}, error={e}"
            )
            return None
    
    async def expire(self, key: str, seconds: int) -> bool:
        """
        设置键过期时间
        
        Args:
            key: 键名
            seconds: 过期时间（秒）
        
        Returns:
            True 表示设置成功，False 表示失败
        """
        try:
            client = await self._get_client()
            result = await client.expire(key, seconds)
            logger.debug(f"Expire set: key={key}, seconds={seconds}, result={result}")
            return result
        except RedisConnectionError as e:
            logger.warning(
                f"Redis connection error while setting expire: key={key}, "
                f"error_type={type(e).__name__}, error={e}"
            )
            return False
        except RedisError as e:
            logger.error(
                f"Failed to set expire: key={key}, "
                f"error_type={type(e).__name__}, error={e}"
            )
            return False
        except Exception as e:
            logger.error(
                f"Unexpected error setting expire: key={key}, "
                f"error_type={type(e).__name__}, error={e}"
            )
            return False
    
    async def delete(self, key: str) -> int:
        """
        删除键
        
        Args:
            key: 键名
        
        Returns:
            删除的键数量（0 表示键不存在或删除失败）
        """
        try:
            client = await self._get_client()
            result = await client.delete(key)
            logger.debug(f"Key deleted: key={key}, result={result}")
            return result
        except RedisConnectionError as e:
            logger.warning(
                f"Redis connection error while deleting key: key={key}, "
                f"error_type={type(e).__name__}, error={e}"
            )
            return 0
        except RedisError as e:
            logger.error(
                f"Failed to delete key: key={key}, "
                f"error_type={type(e).__name__}, error={e}"
            )
            return 0
        except Exception as e:
            logger.error(
                f"Unexpected error deleting key: key={key}, "
                f"error_type={type(e).__name__}, error={e}"
            )
            return 0
    
    async def close(self):
        """
        关闭 Redis 连接
        
        应用退出前调用，确保资源正确释放
        """
        if self._client is not None:
            try:
                await self._client.close()
                self._client = None
                self._connected = False
                logger.info("Redis connection closed")
            except Exception as e:
                logger.error(
                    f"Error closing Redis connection: "
                    f"error_type={type(e).__name__}, error={e}"
                )
    
    def is_connected(self) -> bool:
        """
        检查 Redis 是否已连接
        
        Returns:
            True 表示已连接，False 表示未连接或连接已断开
        """
        return self._connected


# 全局实例
_redis_repo: Optional[RedisRepository] = None


def get_redis_repository() -> RedisRepository:
    """
    获取 Redis 仓库单例
    
    Returns:
        RedisRepository 实例
    
    示例：
        repo = get_redis_repository()
        ok, latency = await repo.ping()
    """
    global _redis_repo
    if _redis_repo is None:
        _redis_repo = RedisRepository()
        logger.debug("已创建 RedisRepository 实例")
    return _redis_repo
