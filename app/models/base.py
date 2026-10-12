"""SQLAlchemy 异步基类与数据库连接管理（含自动重连机制）"""

import asyncio
import logging
import time
from typing import Optional, AsyncGenerator, TypeVar, Callable, Any
from contextlib import asynccontextmanager
from functools import wraps

from sqlalchemy import text, URL
from sqlalchemy.exc import OperationalError, InterfaceError, DisconnectionError
from sqlalchemy.ext.asyncio import (
    create_async_engine,
    AsyncSession,
    AsyncEngine,
    async_sessionmaker,
)
from sqlalchemy.orm import declarative_base
from sqlalchemy.pool import AsyncAdaptedQueuePool

logger = logging.getLogger(__name__)

# 重连配置
DB_MAX_RETRIES = 3  # 最大重试次数
DB_RETRY_BASE_DELAY = 0.5  # 基础重试延迟（秒）
DB_RETRY_MAX_DELAY = 10.0  # 最大重试延迟（秒）

# 数据库连接状态
_db_available = True
_last_db_error: Optional[str] = None

Base = declarative_base()

_engine: Optional[AsyncEngine] = None
_AsyncSessionLocal: Optional[async_sessionmaker[AsyncSession]] = None


def get_engine(
    host: str = "localhost",
    port: int = 3306,
    user: str = "root",
    password: str = "",
    database: str = "server_monitor",
    pool_size: int = 10,
    pool_recycle: int = 1800,
    echo: bool = False,
) -> AsyncEngine:
    """创建或获取异步数据库引擎"""
    global _engine
    if _engine is not None:
        return _engine
    
    # 构建 MySQL 异步连接 URL (使用 aiomysql)
    url = URL.create("mysql+aiomysql", username=user, password=password,
                     host=host, port=port, database=database, query={"charset": "utf8mb4"})
    
    _engine = create_async_engine(
        url,
        poolclass=AsyncAdaptedQueuePool,
        pool_size=pool_size,
        pool_recycle=pool_recycle,
        pool_pre_ping=True,  # 自动检测连接有效性
        echo=echo,
    )
    
    logger.info(f"异步数据库引擎已创建: {host}:{port}/{database}")
    return _engine


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """获取异步数据库会话（用于 FastAPI 依赖注入）"""
    global _AsyncSessionLocal
    if _AsyncSessionLocal is None:
        engine = get_engine()
        _AsyncSessionLocal = async_sessionmaker(
            engine, 
            class_=AsyncSession,
            autocommit=False, 
            autoflush=False,
            expire_on_commit=False,
        )
    
    async with _AsyncSessionLocal() as session:
        yield session


@asynccontextmanager
async def get_session_context() -> AsyncGenerator[AsyncSession, None]:
    """获取异步数据库会话上下文管理器"""
    global _AsyncSessionLocal
    if _AsyncSessionLocal is None:
        engine = get_engine()
        _AsyncSessionLocal = async_sessionmaker(
            engine,
            class_=AsyncSession,
            autocommit=False,
            autoflush=False,
            expire_on_commit=False,
        )
    
    async with _AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def init_db(
    host: str = "localhost",
    port: int = 3306,
    user: str = "root",
    password: str = "",
    database: str = "server_monitor",
    pool_size: int = 10,
    pool_recycle: int = 1800,
    create_tables: bool = True,
) -> AsyncEngine:
    """初始化异步数据库（创建引擎和表）"""
    global _AsyncSessionLocal
    
    engine = get_engine(
        host=host,
        port=port,
        user=user,
        password=password,
        database=database,
        pool_size=pool_size,
        pool_recycle=pool_recycle,
    )
    
    _AsyncSessionLocal = async_sessionmaker(
        engine,
        class_=AsyncSession,
        autocommit=False,
        autoflush=False,
        expire_on_commit=False,
    )
    
    if create_tables:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        logger.info("数据库表已创建/验证")
    
    return engine


async def ping_database() -> tuple[bool, float]:
    """异步检测数据库连接健康状态，返回 (是否成功, 延迟毫秒)"""
    global _engine
    
    if _engine is None:
        return False, 0.0
    
    start = time.perf_counter()
    try:
        async with _engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        latency_ms = (time.perf_counter() - start) * 1000
        return True, latency_ms
    except Exception as e:
        logger.error(f"数据库 ping 失败: {e}")
        latency_ms = (time.perf_counter() - start) * 1000
        return False, latency_ms


async def close_db():
    """关闭数据库连接"""
    global _engine
    if _engine is not None:
        await _engine.dispose()
        _engine = None
        logger.info("Database connection closed")


def is_connection_error(exc: Exception) -> bool:
    """判断是否为可重试的连接错误"""
    # SQLAlchemy 连接相关异常
    if isinstance(exc, (OperationalError, InterfaceError, DisconnectionError)):
        return True
    
    # 检查异常消息中的关键字
    error_msg = str(exc).lower()
    connection_error_keywords = [
        "connection",
        "lost",
        "gone away",
        "can't connect",
        "connection refused",
        "connection reset",
        "network",
        "timeout",
        "timed out",
        "broken pipe",
        "server closed",
    ]
    return any(kw in error_msg for kw in connection_error_keywords)


async def _execute_with_retry(
    operation: Callable[[], Any],
    max_retries: int = DB_MAX_RETRIES,
    base_delay: float = DB_RETRY_BASE_DELAY,
    max_delay: float = DB_RETRY_MAX_DELAY,
) -> Any:
    """执行数据库操作，失败时自动重试（指数退避）"""
    global _db_available, _last_db_error
    
    last_exception = None
    
    for attempt in range(max_retries + 1):
        try:
            result = await operation()
            # 成功后恢复状态
            if not _db_available:
                _db_available = True
                _last_db_error = None
                logger.info("Database connection recovered")
            return result
        except Exception as e:
            last_exception = e
            
            if not is_connection_error(e):
                # 非连接错误，直接抛出
                raise
            
            if attempt < max_retries:
                # 计算指数退避延迟
                delay = min(base_delay * (2 ** attempt), max_delay)
                logger.warning(
                    f"Database connection error (attempt {attempt + 1}/{max_retries + 1}): {e}. "
                    f"Retrying in {delay:.1f}s..."
                )
                await asyncio.sleep(delay)
            else:
                # 达到最大重试次数
                _db_available = False
                _last_db_error = str(e)
                logger.error(
                    f"Database connection failed after {max_retries + 1} attempts: {e}"
                )
                raise
    
    # 不应该到达这里，但为了安全
    raise last_exception  # type: ignore


@asynccontextmanager
async def get_session_with_retry() -> AsyncGenerator[AsyncSession, None]:
    """
    获取异步数据库会话（带自动重连）
    
    自动处理连接错误并进行重试。适用于需要更高可靠性的场景。
    """
    global _AsyncSessionLocal
    
    if _AsyncSessionLocal is None:
        engine = get_engine()
        _AsyncSessionLocal = async_sessionmaker(
            engine,
            class_=AsyncSession,
            autocommit=False,
            autoflush=False,
            expire_on_commit=False,
        )
    
    async def create_session():
        return _AsyncSessionLocal()
    
    session = await _execute_with_retry(create_session)
    try:
        yield session
        
        async def commit_session():
            await session.commit()
        
        await _execute_with_retry(commit_session)
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()


def get_db_status() -> dict:
    """获取数据库连接状态（用于健康检查）"""
    return {
        "available": _db_available,
        "last_error": _last_db_error,
    }


async def reconnect_db() -> bool:
    """
    尝试重新建立数据库连接
    
    当检测到数据库不可用时，可以调用此方法尝试重连。
    返回 True 表示重连成功，False 表示失败。
    """
    global _engine, _AsyncSessionLocal, _db_available, _last_db_error
    
    logger.info("正在尝试重新连接数据库...")
    
    try:
        # 如果有旧引擎，先关闭
        if _engine is not None:
            try:
                await _engine.dispose()
            except Exception as e:
                logger.warning(f"关闭旧引擎时出错: {e}")
            _engine = None
            _AsyncSessionLocal = None
        
        # 尝试 ping 新连接
        success, latency = await ping_database()
        
        if success:
            _db_available = True
            _last_db_error = None
            logger.info(f"数据库重连成功 (延迟: {latency:.1f}ms)")
            return True
        else:
            _db_available = False
            _last_db_error = "Ping failed"
            logger.error("数据库重连失败: ping 未成功")
            return False
    except Exception as e:
        _db_available = False
        _last_db_error = str(e)
        logger.error(f"数据库重连失败: {e}")
        return False
