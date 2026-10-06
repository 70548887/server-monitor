"""
Redis 客户端快速失败测试

背景：Redis 在本项目中是**可选组件**，不可用时降级到内存实现。
redis-py 默认带重试与指数退避，实测 Redis 宕机时单次命令阻塞约 25 秒，
导致：
    - 每个请求都卡 25 秒
    - 限流降级到内存前先被拖死
    - k8s 健康探针超时 → Pod 反复重启

这些用例锁定"Redis 不可用时必须快速失败"这一约束。
"""

import asyncio
import time

import pytest
import redis.asyncio as aioredis


class TestRedisClientConfig:
    """客户端构造参数"""

    @pytest.fixture
    def client_kwargs(self):
        """复现仓库实际使用的构造参数"""
        from app.services.config import get_config

        config = get_config()
        return dict(
            host=config.database.redis_host or "127.0.0.1",
            port=config.database.redis_port,
            db=config.database.redis_db,
            decode_responses=True,
        )

    async def test_retries_disabled(self, client_kwargs):
        """必须关闭重试——这是 25 秒阻塞的直接来源"""
        from app.repositories.redis_repository import get_redis_repository

        repo = get_redis_repository()
        client = await repo._get_client()

        connection_kwargs = client.connection_pool.connection_kwargs
        retry = connection_kwargs.get("retry")
        assert retry is not None, "未显式配置 retry，redis-py 会用带退避的默认值"
        # Retry(NoBackoff(), 0) 的 retries 为 0
        assert getattr(retry, "_retries", None) == 0, (
            f"重试次数应为 0，实际 {getattr(retry, '_retries', None)}"
        )

    async def test_connect_timeout_is_short(self, client_kwargs):
        """连接超时必须足够短"""
        from app.repositories.redis_repository import get_redis_repository

        repo = get_redis_repository()
        client = await repo._get_client()
        kwargs = client.connection_pool.connection_kwargs

        assert kwargs.get("socket_connect_timeout") == 1
        assert kwargs.get("socket_timeout") == 1

    async def test_retry_on_timeout_disabled(self):
        """
        超时不重试

        retry_on_timeout 不在连接池参数里（那是 Redis() 构造参数，
        存入客户端的 retry 策略），因此这里断言"重试次数为 0"即可 ——
        它才是阻止超时重试生效的关键。
        """
        from app.repositories.redis_repository import get_redis_repository

        repo = get_redis_repository()
        client = await repo._get_client()
        kwargs = client.connection_pool.connection_kwargs

        retry = kwargs.get("retry")
        assert getattr(retry, "_retries", None) == 0, (
            "重试次数必须为 0，否则超时会被反复重试放大"
        )


class TestRedisFailsFast:
    """
    Redis 不可用时必须快速失败

    这些测试需要一个**不可达**的 Redis 地址。用 127.0.0.1 上一个
    高端口（几乎不可能有服务在监听）来模拟"连接被拒绝"。
    """

    UNREACHABLE_PORT = 6399

    async def test_command_fails_within_2s(self):
        """单条命令在 2 秒内失败，而不是数十秒"""
        client = aioredis.Redis(
            host="127.0.0.1",
            port=self.UNREACHABLE_PORT,
            decode_responses=True,
            socket_connect_timeout=1,
            socket_timeout=1,
            retry=_no_retry(),
            retry_on_timeout=False,
        )
        try:
            start = time.perf_counter()
            with pytest.raises(Exception):
                await client.incr("t:fast")
            elapsed = time.perf_counter() - start

            assert elapsed < 2.0, f"失败耗时 {elapsed:.2f} 秒，超过 2 秒上限"
        finally:
            await client.aclose()

    async def test_repository_method_fails_within_3s(self):
        """仓库方法（带异常兜底）也应快速返回 0"""
        from app.repositories import redis_repository as repo_mod

        repo = repo_mod.RedisRepository()
        repo._client = aioredis.Redis(
            host="127.0.0.1",
            port=self.UNREACHABLE_PORT,
            decode_responses=True,
            socket_connect_timeout=1,
            socket_timeout=1,
            retry=_no_retry(),
            retry_on_timeout=False,
        )

        start = time.perf_counter()
        result = await repo.increment_error_count("test", 60)
        elapsed = time.perf_counter() - start

        # 方法吞掉异常并返回 0
        assert result == 0
        assert elapsed < 3.0, f"耗时 {elapsed:.2f} 秒，超过 3 秒上限"

        await repo._client.aclose()


def _no_retry():
    from redis.backoff import NoBackoff
    from redis.retry import Retry

    return Retry(NoBackoff(), 0)


class TestRateLimitDegradation:
    """限流在 Redis 不可用时的降级行为"""

    async def test_rate_limit_falls_back_to_memory(self, monkeypatch):
        """
        Redis 故障时限流降级到内存，且不阻塞

        这是 SecurityMiddleware 每个请求都会走的路径。
        """
        from app.middleware import security as sec_mod

        store = sec_mod.RateLimitStore()
        # 强制走 Redis 分支（不可达地址）
        store._redis_available = True
        monkeypatch.setattr(
            sec_mod,
            "get_redis_repository",
            lambda: _UnreachableRedis(),
        )

        start = time.perf_counter()
        limited = await store.is_rate_limited("1.2.3.4:default", 100, 60)
        elapsed = time.perf_counter() - start

        assert limited is False, "首次请求不应被限流"
        assert elapsed < 3.0, (
            f"Redis 不可用时限流检查耗时 {elapsed:.2f} 秒，"
            "会让每个请求都被阻塞"
        )

    async def test_subsequent_calls_use_memory(self, monkeypatch):
        """首次失败后应切到内存，后续不再碰 Redis"""
        from app.middleware import security as sec_mod

        store = sec_mod.RateLimitStore()
        store._redis_available = True
        monkeypatch.setattr(
            sec_mod, "get_redis_repository", lambda: _UnreachableRedis()
        )

        await store.is_rate_limited("5.6.7.8:default", 100, 60)

        assert store._redis_available is False, (
            "Redis 失败后未标记不可用，后续请求会重复超时"
        )

        # 后续调用走内存，应当很快
        start = time.perf_counter()
        await store.is_rate_limited("5.6.7.8:default", 100, 60)
        elapsed = time.perf_counter() - start
        assert elapsed < 0.5, f"内存路径耗时 {elapsed:.3f} 秒，不应再触碰 Redis"


class _UnreachableRedis:
    """模拟连不上的 Redis 仓库"""

    async def increment_error_count(self, key, window):
        # 与真实客户端同样的失败语义
        raise ConnectionError("模拟 Redis 不可用")