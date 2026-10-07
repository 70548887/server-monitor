"""
数据库引导脚本 - 建库 + 建表 + Phase 2 迁移

用途：
    在导入数据之前，先把库与表结构准备好。本脚本**不插入任何业务数据**，
    数据由使用者自行导入（节点、通知渠道、管理员等）。

凭据来源：
    全部取自应用自身配置（configs/app.yaml + .env 环境变量），
    脚本内不硬编码任何用户名或密码。

执行：
    python scripts/bootstrap_database.py

幂等：
    可重复执行。已存在的库/表不会被删除或重建；
    Phase 2 的补列语句只在缺列时才执行。
"""

import asyncio
import sys
from pathlib import Path

# 确保项目根目录可导入
ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

# 导入全部模型，确保 Base.metadata 注册了所有表
import app.models.admin  # noqa: F401
import app.models.node  # noqa: F401
import app.models.sample  # noqa: F401
import app.models.notification_channel  # noqa: F401

from app.models.base import Base, get_engine
from app.services.config import load_config


def log(msg: str):
    print(msg, flush=True)


async def create_database_if_missing(config) -> bool:
    """
    创建数据库（若不存在）

    引擎不能直接连到不存在的库，因此先连到服务器再 CREATE DATABASE。
    """
    db = config.database
    # 不指定库名，连接到服务器
    url = (
        f"mysql+aiomysql://{db.mysql_user}:{db.mysql_password}"
        f"@{db.mysql_host}:{db.mysql_port}"
    )
    engine = create_async_engine(url, connect_args={"connect_timeout": 10})
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    f"CREATE DATABASE IF NOT EXISTS `{db.mysql_database}` "
                    "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
                )
            )
        log(f"  [OK] 数据库 `{db.mysql_database}` 就绪")
        return True
    finally:
        await engine.dispose()


async def create_tables(config) -> int:
    """按 ORM 模型建表（已存在的表不会被改动）"""
    engine = get_engine(
        host=config.database.mysql_host,
        port=config.database.mysql_port,
        user=config.database.mysql_user,
        password=config.database.mysql_password,
        database=config.database.mysql_database,
    )
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        async with engine.connect() as conn:
            result = await conn.execute(text("SHOW TABLES"))
            tables = [row[0] for row in result.fetchall()]
        return len(tables)
    finally:
        await engine.dispose()


async def column_exists(conn, table: str, column: str) -> bool:
    """检查列是否存在"""
    result = await conn.execute(
        text(
            "SELECT COUNT(*) FROM information_schema.columns "
            "WHERE table_schema = DATABASE() AND table_name = :t AND column_name = :c"
        ),
        {"t": table, "c": column},
    )
    return result.scalar() > 0


async def run_phase2_migration(config) -> int:
    """
    Phase 2 迁移：alert_incidents 表 + 补列

    Base.metadata.create_all 只创建"不存在的表"，不会给已存在的表补列。
    所以 alert_logs / notification_logs 的新列必须显式 ALTER。
    """
    from app.models.notification_channel import AlertIncidentModel

    engine = get_engine(
        host=config.database.mysql_host,
        port=config.database.mysql_port,
        user=config.database.mysql_user,
        password=config.database.mysql_password,
        database=config.database.mysql_database,
    )
    added = 0
    try:
        async with engine.begin() as conn:
            # 1. alert_incidents 表
            exists = await conn.execute(
                text(
                    "SELECT COUNT(*) FROM information_schema.tables "
                    "WHERE table_schema = DATABASE() AND table_name = 'alert_incidents'"
                )
            )
            if exists.scalar() == 0:
                await conn.run_sync(AlertIncidentModel.__table__.create)
                log("  [新增] 表 alert_incidents")
                added += 1
            else:
                log("  [已存在] 表 alert_incidents")

            # 2. 补列
            for table, col, ddl in [
                (
                    "alert_logs", "incident_id",
                    "ALTER TABLE alert_logs ADD COLUMN incident_id INT NULL "
                    "COMMENT '所属故障事件ID'",
                ),
                (
                    "alert_logs", "notify_seq",
                    "ALTER TABLE alert_logs ADD COLUMN notify_seq INT NULL "
                    "COMMENT '该事件内第几次提醒(从1开始)'",
                ),
                (
                    "notification_logs", "incident_id",
                    "ALTER TABLE notification_logs ADD COLUMN incident_id INT NULL "
                    "COMMENT '所属故障事件ID'",
                ),
                (
                    "notification_logs", "notify_seq",
                    "ALTER TABLE notification_logs ADD COLUMN notify_seq INT NULL "
                    "COMMENT '该事件内第几次提醒(从1开始)'",
                ),
            ]:
                if await column_exists(conn, table, col):
                    log(f"  [已存在] {table}.{col}")
                else:
                    await conn.execute(text(ddl))
                    log(f"  [新增] {table}.{col}")
                    added += 1

            # 3. 索引（MySQL 不支持 CREATE INDEX IF NOT EXISTS）
            for index, ddl in [
                (
                    "ix_alert_incidents_next_notify_at",
                    "CREATE INDEX ix_alert_incidents_next_notify_at "
                    "ON alert_incidents (next_notify_at)",
                ),
                (
                    "ix_notification_logs_incident_id",
                    "CREATE INDEX ix_notification_logs_incident_id "
                    "ON notification_logs (incident_id)",
                ),
                (
                    "ix_alert_logs_incident_id",
                    "CREATE INDEX ix_alert_logs_incident_id "
                    "ON alert_logs (incident_id)",
                ),
            ]:
                result = await conn.execute(
                    text(
                        "SELECT COUNT(*) FROM information_schema.statistics "
                        "WHERE table_schema = DATABASE() AND index_name = :i"
                    ),
                    {"i": index},
                )
                if result.scalar() == 0:
                    await conn.execute(text(ddl))
                    log(f"  [新增] 索引 {index}")
                    added += 1
                else:
                    log(f"  [已存在] 索引 {index}")

        return added
    finally:
        await engine.dispose()


async def show_status(config) -> None:
    """输出各表的数据量，便于确认导入情况"""
    engine = get_engine(
        host=config.database.mysql_host,
        port=config.database.mysql_port,
        user=config.database.mysql_user,
        password=config.database.mysql_password,
        database=config.database.mysql_database,
    )
    try:
        async with engine.connect() as conn:
            result = await conn.execute(text("SHOW TABLES"))
            tables = [row[0] for row in result.fetchall()]

            log("")
            log("  表数据量：")
            for t in sorted(tables):
                try:
                    r = await conn.execute(text(f"SELECT COUNT(*) FROM `{t}`"))
                    n = r.scalar()
                except Exception:
                    n = "?"
                log(f"    {t:24s} {n} 行")
    finally:
        await engine.dispose()


async def main() -> int:
    log("=" * 60)
    log("数据库引导")
    log("=" * 60)

    config = load_config()

    log("")
    log("[1/4] 连接 MySQL ...")
    log(f"      {config.database.mysql_host}:{config.database.mysql_port}"
        f"  用户 {config.database.mysql_user}")
    log(f"      目标库: {config.database.mysql_database}")

    log("")
    log("[2/4] 创建数据库（若不存在）...")
    try:
        await create_database_if_missing(config)
    except Exception as e:
        msg = str(e)
        log(f"  [失败] {type(e).__name__}: {msg[:160]}")
        log("")

        if "using password: NO" in msg:
            # 驱动连上了，但发出的请求没有密码 —— 几乎总是配置缺失
            log("  诊断：MySQL 已连通，但客户端未提交密码。")
            log("        即 .env 中的 MYSQL_PASSWORD 为空或未设置。")
            log("")
            log("  请在项目根目录的 .env 中补上：")
            log(f"    MYSQL_HOST={config.database.mysql_host}")
            log(f"    MYSQL_PORT={config.database.mysql_port}")
            log(f"    MYSQL_USER={config.database.mysql_user}")
            log("    MYSQL_PASSWORD=<你的密码>")
            log(f"    MYSQL_DATABASE={config.database.mysql_database}")
        elif "1045" in msg or "Access denied" in msg:
            log("  诊断：账号或密码不正确。")
            log("        请核对 .env 中 MYSQL_USER / MYSQL_PASSWORD。")
        elif "Can't connect" in msg or "2003" in msg:
            log("  诊断：连不上 MySQL 服务。")
            log(f"        请确认 {config.database.mysql_host}:"
                f"{config.database.mysql_port} 上服务已启动。")
        else:
            log("  请检查 .env 中的 MySQL 配置是否正确。")
        return 1

    log("")
    log("[3/4] 创建表结构（按 ORM 模型）...")
    try:
        count = await create_tables(config)
        log(f"  [OK] 共 {count} 张表")
    except Exception as e:
        log(f"  [失败] {type(e).__name__}: {str(e)[:160]}")
        return 1

    log("")
    log("[4/4] Phase 2 迁移（事件表与补列）...")
    try:
        added = await run_phase2_migration(config)
        log(f"  完成，本次新增 {added} 项")
    except Exception as e:
        log(f"  [失败] {type(e).__name__}: {str(e)[:160]}")
        return 1

    await show_status(config)

    log("")
    log("=" * 60)
    log("完成。库与表结构已就绪，可导入数据。")
    log("")
    log("导入建议顺序（避免外键/逻辑依赖问题）：")
    log("  1. admins             管理员账户（否则无法登录）")
    log("  2. nodes              监控节点")
    log("  3. notification_channels / notification_recipients  通知配置")
    log("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))