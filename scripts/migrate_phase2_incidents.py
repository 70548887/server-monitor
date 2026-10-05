"""
数据库迁移 - Phase 2 事件化告警提醒

背景：
    项目使用 Base.metadata.create_all 建表，它只会创建"不存在的表"，
    对已存在的表不会补列。因此 alert_logs 的新列与 alert_incidents
    新表都需要一次性手工迁移。

本脚本是幂等的：重复执行不会报错，也不会丢数据。

用法:
    python scripts/migrate_phase2_incidents.py

执行内容:
    1. 建表 alert_incidents（故障事件）
    2. notification_logs 增加 incident_id / notify_seq 两列
    3. alert_logs 增加 incident_id / notify_seq 两列
"""

import asyncio
import logging
import sys
from pathlib import Path

# 确保项目根目录在路径中
sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import text, inspect

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger("migrate")


async def table_exists(conn, table_name: str) -> bool:
    """检查表是否存在"""
    def _check(sync_conn):
        return inspect(sync_conn).has_table(table_name)
    return await conn.run_sync(_check)


async def column_exists(conn, table_name: str, column_name: str) -> bool:
    """检查列是否存在"""
    def _check(sync_conn):
        cols = inspect(sync_conn).get_columns(table_name)
        return any(c["name"] == column_name for c in cols)
    return await conn.run_sync(_check)


async def migrate():
    from app.models.base import get_engine, init_db
    from app.services.config import get_config
    import app.models.notification_channel  # noqa: F401  确保模型已注册
    from app.models.base import Base

    config = get_config()

    # 先建立引擎并创建任何缺失的表（含 alert_incidents）
    engine = await init_db(
        host=config.database.mysql_host,
        port=config.database.mysql_port,
        user=config.database.mysql_user,
        password=config.database.mysql_password,
        database=config.database.mysql_database,
        pool_size=config.database.mysql_pool_size,
        pool_recycle=config.database.mysql_pool_recycle,
        create_tables=True,
    )

    if engine is None:
        logger.error("无法建立数据库连接，请检查 .env 中的 MySQL 配置")
        return 1

    async with engine.begin() as conn:
        # ---------- 1. alert_incidents 表 ----------
        if await table_exists(conn, "alert_incidents"):
            logger.info("表 alert_incidents 已存在，跳过建表")
        else:
            await conn.run_sync(
                Base.metadata.tables["alert_incidents"].create
            )
            logger.info("已创建表 alert_incidents")

        # ---------- 2. notification_logs 补列 ----------
        for col, ddl in [
            ("incident_id", "ALTER TABLE notification_logs ADD COLUMN incident_id INT NULL COMMENT '所属故障事件ID'"),
            ("notify_seq", "ALTER TABLE notification_logs ADD COLUMN notify_seq INT NULL COMMENT '该事件内第几次提醒(从1开始)'"),
        ]:
            if await column_exists(conn, "notification_logs", col):
                logger.info(f"notification_logs.{col} 已存在，跳过")
            else:
                await conn.execute(text(ddl))
                logger.info(f"已为 notification_logs 添加列 {col}")

        # ---------- 3. alert_logs 补列 ----------
        for col, ddl in [
            ("incident_id", "ALTER TABLE alert_logs ADD COLUMN incident_id INT NULL COMMENT '所属故障事件ID'"),
            ("notify_seq", "ALTER TABLE alert_logs ADD COLUMN notify_seq INT NULL COMMENT '该事件内第几次提醒(从1开始)'"),
        ]:
            if await column_exists(conn, "alert_logs", col):
                logger.info(f"alert_logs.{col} 已存在，跳过")
            else:
                await conn.execute(text(ddl))
                logger.info(f"已为 alert_logs 添加列 {col}")

        # ---------- 4. 索引 ----------
        # 检查索引是否已存在（MySQL 不支持 CREATE INDEX IF NOT EXISTS）
        async def index_exists(table: str, index: str) -> bool:
            def _check(sync_conn):
                idxs = inspect(sync_conn).get_indexes(table)
                return any(i["name"] == index for i in idxs)
            return await conn.run_sync(_check)

        for table, index, ddl in [
            ("notification_logs", "ix_notification_logs_incident_id",
             "CREATE INDEX ix_notification_logs_incident_id ON notification_logs (incident_id)"),
        ]:
            if await index_exists(table, index):
                logger.info(f"索引 {index} 已存在，跳过")
            else:
                await conn.execute(text(ddl))
                logger.info(f"已创建索引 {index}")

    logger.info("Phase 2 迁移完成")

    # 打印当前表结构确认
    async with engine.connect() as conn:
        def _cols(sync_conn, table):
            return [c["name"] for c in inspect(sync_conn).get_columns(table)]

        cols_inc = await conn.run_sync(_cols, "alert_incidents")
        cols_nlog = await conn.run_sync(_cols, "notification_logs")
        cols_alog = await conn.run_sync(_cols, "alert_logs")

    logger.info(f"alert_incidents 列: {', '.join(cols_inc)}")
    logger.info(f"notification_logs 新增列: {[c for c in cols_nlog if c in ('incident_id', 'notify_seq')]}")
    logger.info(f"alert_logs 新增列: {[c for c in cols_alog if c in ('incident_id', 'notify_seq')]}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(migrate()))
