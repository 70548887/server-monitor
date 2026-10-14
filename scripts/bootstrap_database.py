"""
数据库引导脚本 - 建库 + 建表 + 已发布增量升级

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
    与应用启动共用升级流程，补列语句只在缺列时才执行。
"""

import asyncio
import sys
from pathlib import Path

# 确保项目根目录可导入
ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from sqlalchemy import text, URL
from sqlalchemy.ext.asyncio import create_async_engine

# 导入全部模型，确保 Base.metadata 注册了所有表
import app.models.admin  # noqa: F401
import app.models.node  # noqa: F401
import app.models.sample  # noqa: F401
import app.models.notification_channel  # noqa: F401

from app.models.base import get_engine
from app.models.schema import ensure_schema
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
    url = URL.create("mysql+aiomysql", username=db.mysql_user, password=db.mysql_password,
                     host=db.mysql_host, port=db.mysql_port)
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
    """使用与应用启动相同的锁、建表和增量升级流程。"""
    engine = get_engine(
        host=config.database.mysql_host,
        port=config.database.mysql_port,
        user=config.database.mysql_user,
        password=config.database.mysql_password,
        database=config.database.mysql_database,
    )
    try:
        await ensure_schema(engine, report=log)

        async with engine.connect() as conn:
            result = await conn.execute(text("SHOW TABLES"))
            tables = [row[0] for row in result.fetchall()]
        return len(tables)
    finally:
        await engine.dispose()


async def run_phase2_migration(config) -> int:
    """兼容旧调用入口；增量升级与应用启动共用同一实现和升级锁。"""
    engine = get_engine(
        host=config.database.mysql_host,
        port=config.database.mysql_port,
        user=config.database.mysql_user,
        password=config.database.mysql_password,
        database=config.database.mysql_database,
    )
    try:
        return await ensure_schema(engine, create_tables=False, report=log)
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
    log("[3/4] 检查并补齐表结构（与应用启动相同）...")
    try:
        count = await create_tables(config)
        log(f"  [OK] 共 {count} 张表")
    except Exception as e:
        log(f"  [失败] {type(e).__name__}: {str(e)[:160]}")
        return 1

    log("")
    log("[4/4] 检查数据库状态...")
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
