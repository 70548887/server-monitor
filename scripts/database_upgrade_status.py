"""只读查看数据库升级锁和大表转换状态，不创建或修改库表。"""
import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import re
import sys

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sqlalchemy import URL, text
from sqlalchemy.ext.asyncio import create_async_engine

from app.services.config import load_config

TABLES = ("nodes", "samples", "aggregates", "alert_incidents", "alert_logs", "notification_logs")


async def inspect_upgrade(engine):
    async with engine.connect() as connection:
        database = (await connection.execute(text("SELECT DATABASE()"))).scalar()
        if not database:
            raise RuntimeError("未选择数据库")
        lock_name = "server_monitor_schema_" + hashlib.sha256(database.encode()).hexdigest()[:40]
        owner = (await connection.execute(text("SELECT IS_USED_LOCK(:name)"), {"name": lock_name})).scalar()
        tables = (await connection.execute(text(
            "SELECT TABLE_NAME, ENGINE, TABLE_ROWS, DATA_LENGTH, INDEX_LENGTH "
            "FROM information_schema.tables WHERE table_schema = DATABASE() "
            "AND table_name IN ('nodes','samples','aggregates','alert_incidents','alert_logs','notification_logs') "
            "ORDER BY TABLE_NAME"
        ))).mappings().all()
        processes = []
        process_visibility = "visible_connections_only"
        try:
            rows = (await connection.execute(text(
                "SELECT ID, COMMAND, TIME, STATE, INFO FROM information_schema.PROCESSLIST "
                "WHERE DB = DATABASE() AND (ID = :owner OR INFO LIKE 'ALTER TABLE%') LIMIT 30"
            ), {"owner": owner or 0})).mappings().all()
            for row in rows:
                sql = str(row["INFO"] or "")
                match = re.match(r"\s*ALTER\s+TABLE\s+`?([a-zA-Z_]+)`?", sql, re.I)
                table = match.group(1).lower() if match else None
                if row["ID"] != owner and table not in TABLES:
                    continue
                processes.append({
                    "connection_id": int(row["ID"]), "command": row["COMMAND"],
                    "elapsed_seconds": int(row["TIME"] or 0), "state": str(row["STATE"] or "")[:200],
                    "table": table if table in TABLES else None,
                    "engine_conversion": bool(table in TABLES and re.search(r"ENGINE\s*=\s*InnoDB", sql, re.I)),
                })
        except Exception:
            # 某些应用账号只能看到自己的连接，缺失不等同于没有迁移。
            process_visibility = "unavailable_or_permission_limited"
        table_list = [{
            "table": row["TABLE_NAME"], "engine": row["ENGINE"],
            "estimated_rows": int(row["TABLE_ROWS"] or 0),
            "size_mb": round((int(row["DATA_LENGTH"] or 0) + int(row["INDEX_LENGTH"] or 0)) / 1048576, 2),
        } for row in tables]
        converting = any(row["engine_conversion"] for row in processes)
        pending = [row["table"] for row in table_list if str(row["engine"]).lower() != "innodb"]
        if converting:
            advice = "正在转换旧表，请等待完成；关闭宝塔自动重启，避免重复拉起新进程，不强制结束转换SQL。"
        elif owner:
            advice = "另一连接仍持有升级锁；当前账号可能看不到完整进程，请先核查该连接，暂不重复运行迁移。"
        elif pending:
            advice = "未发现升级锁，仍有旧引擎表。关闭项目自动重启后，仅运行一次 scripts/bootstrap_database.py，完成后再启动项目。"
        else:
            advice = "未发现升级锁，已存在的关联表均为InnoDB；可手动启动一个项目实例。"
        return {"database": database, "upgrade_lock_owner": owner,
                "process_visibility": process_visibility, "processes": processes,
                "tables": table_list, "tables_requiring_conversion": pending, "advice": advice}


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    config = load_config(app_config_path=str(ROOT / "configs/app.yaml"), nodes_config_path=str(ROOT / "configs/nodes.yaml"))
    db = config.database
    url = URL.create("mysql+aiomysql", username=db.mysql_user, password=db.mysql_password,
                     host=db.mysql_host, port=db.mysql_port, database=db.mysql_database)
    engine = create_async_engine(url, hide_parameters=True, connect_args={"connect_timeout": 10})
    try:
        result = await asyncio.wait_for(inspect_upgrade(engine), timeout=20)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    finally:
        await engine.dispose()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as error:
        # 不输出包含连接URL或配置的异常正文。
        print(json.dumps({"error_type": type(error).__name__, "message": "读取失败，请检查MySQL连接或元数据查询权限；未修改数据库"}, ensure_ascii=False))
        raise SystemExit(1)
