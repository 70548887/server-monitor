"""已发布数据库增量升级；启动和手工引导共用，不改写业务记录。"""

import hashlib
import logging
from contextlib import asynccontextmanager
from time import perf_counter
from typing import Callable, Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

logger = logging.getLogger(__name__)

# 节点名称作为历史数据关联键；重命名必须让这些表在同一事务内提交或回滚。
NODE_TRANSACTION_TABLES = (
    "nodes", "samples", "aggregates", "alert_incidents", "alert_logs", "notification_logs",
)


class SchemaMigrationError(RuntimeError):
    """结构未就绪时禁止启动调度器或开放业务接口。"""


@asynccontextmanager
async def schema_lock(conn):
    """MySQL DDL 会隐式提交，用连接级命名锁串行化多个 worker 的升级。"""
    database = (await conn.execute(text("SELECT DATABASE()"))).scalar()
    if not database:
        raise SchemaMigrationError("未选择数据库，无法检查表结构")
    # MySQL 命名锁上限 64 字节；按库隔离，所有升级入口使用相同名字。
    lock_name = "server_monitor_schema_" + hashlib.sha256(database.encode()).hexdigest()[:40]
    acquired = (await conn.execute(
        text("SELECT GET_LOCK(:name, :timeout)"),
        {"name": lock_name, "timeout": 30},
    )).scalar()
    if acquired != 1:
        raise SchemaMigrationError("等待数据库结构升级锁超时或失败，请稍后重启")
    try:
        yield
    finally:
        try:
            released = (await conn.execute(
                text("SELECT RELEASE_LOCK(:name)"), {"name": lock_name},
            )).scalar()
            if released != 1:
                raise RuntimeError("数据库结构升级锁未正常释放")
        except BaseException:
            # 不把仍可能持有命名锁的连接放回连接池。
            await conn.invalidate()
            raise


async def column_exists(conn, table: str, column: str) -> bool:
    result = await conn.execute(text(
        "SELECT COUNT(*) FROM information_schema.columns "
        "WHERE table_schema = DATABASE() AND table_name = :t AND column_name = :c"
    ), {"t": table, "c": column})
    return result.scalar() > 0


async def _upgrade_incident_schema(conn, report: Callable[[str], None]) -> int:
    """只补齐已知版本变更，保留历史日志和现有事件的所有值。"""
    from app.models.notification_channel import AlertIncidentModel

    added = 0
    exists = (await conn.execute(text(
        "SELECT COUNT(*) FROM information_schema.tables "
        "WHERE table_schema = DATABASE() AND table_name = 'alert_incidents'"
    ))).scalar()
    if not exists:
        await conn.run_sync(AlertIncidentModel.__table__.create)
        report("新增表 alert_incidents")
        added += 1

    for table in ("alert_logs", "notification_logs"):
        for column, comment in (
            ("incident_id", "所属故障事件ID"),
            ("notify_seq", "该事件内第几次提醒(从1开始)"),
        ):
            if not await column_exists(conn, table, column):
                # 标识符和备注全部来自上述固定列表，不能传入用户输入。
                await conn.execute(text(
                    f"ALTER TABLE {table} ADD COLUMN {column} INT NULL COMMENT '{comment}'"
                ))
                report(f"新增列 {table}.{column}")
                added += 1

    for table, column in (
        ("alert_incidents", "next_notify_at"),
        ("notification_logs", "incident_id"),
        ("alert_logs", "incident_id"),
    ):
        index = f"ix_{table}_{column}"
        result = await conn.execute(text(
            "SELECT COUNT(*) FROM information_schema.statistics "
            "WHERE table_schema = DATABASE() AND table_name = :t AND index_name = :i"
        ), {"t": table, "i": index})
        if not result.scalar():
            await conn.execute(text(f"CREATE INDEX {index} ON {table} ({column})"))
            report(f"新增索引 {index}")
            added += 1
    return added


async def _ensure_node_transaction_engines(conn, report: Callable[[str], None]) -> int:
    """旧导入库可能使用 MyISAM；在启动升级锁内逐表补齐事务支持。"""
    converted = 0
    for table in NODE_TRANSACTION_TABLES:
        engine_name = (await conn.execute(text(
            "SELECT ENGINE FROM information_schema.tables "
            "WHERE table_schema = DATABASE() AND table_name = :t"
        ), {"t": table})).scalar()
        # create_tables=False 的手动升级可运行于部分旧库，缺表仍由建表入口负责。
        if engine_name is None or str(engine_name).lower() == "innodb":
            continue
        started = perf_counter()
        report(f"正在将 {table} 从 {engine_name} 转为 InnoDB，数据较多时需要等待")
        # 表名仅来自上面的固定列表，不接受用户输入。
        await conn.execute(text(f"ALTER TABLE {table} ENGINE=InnoDB"))
        report(f"{table} 已转为 InnoDB，耗时 {perf_counter() - started:.2f} 秒")
        converted += 1
    return converted


async def ensure_schema(
    engine: AsyncEngine,
    *,
    create_tables: bool = True,
    report: Optional[Callable[[str], None]] = None,
) -> int:
    """检查和补齐表结构；仅完成全部升级后返回，重复执行不重复 DDL。

    create_tables=False 供旧引导脚本仅执行已发布增量升级，仍需升级锁。
    init_db(create_tables=False) 不调用本函数，也不会产生任何 DDL。
    """
    from app.models.base import Base
    import app.models.admin  # noqa: F401 — 注册全部模型，包括管理员

    report = report or logger.info
    # 连接失败保留原异常，应用可按既有策略降级；连接成功后的结构错误必须阻断启动。
    async with engine.connect() as conn:
        try:
            async with schema_lock(conn):
                if create_tables:
                    await conn.run_sync(Base.metadata.create_all)
                added = await _upgrade_incident_schema(conn, report)
                added += await _ensure_node_transaction_engines(conn, report)
                await conn.commit()
        except Exception as exc:
            if isinstance(exc, SchemaMigrationError):
                raise
            raise SchemaMigrationError(
                "数据库结构自动升级失败，请检查 MySQL 建表/改表权限及启动日志后重启"
            ) from exc
    logger.info("数据库表结构检查完成，本次补齐 %s 项", added)
    return added
