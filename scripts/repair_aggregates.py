"""备份事件/聚合后，从仍保留的原始样本重建完整5分钟窗口。

默认只预览。执行：python scripts/repair_aggregates.py --apply
仅替换样本确实覆盖的节点窗口，不按数据年龄删除历史统计。
"""
import argparse
import asyncio
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import text
from app.models.base import init_db, close_db
from app.services.config import get_config
from app.repositories.mysql_repository import MySQLRepository
from app.utils.timeutil import utcnow


async def run(apply=False):
    c = get_config().database
    engine = await init_db(host=c.mysql_host, port=c.mysql_port, user=c.mysql_user,
                           password=c.mysql_password, database=c.mysql_database,
                           create_tables=False)
    try:
        async with engine.connect() as conn:
            first, last = (await conn.execute(text("SELECT MIN(timestamp), MAX(timestamp) FROM samples"))).one()
            count = (await conn.execute(text("SELECT COUNT(*) FROM aggregates"))).scalar_one()
            event_engine = (await conn.execute(text("SELECT ENGINE FROM information_schema.tables WHERE table_schema=DATABASE() AND table_name='alert_incidents'"))).scalar_one()
        print(json.dumps({"sample_start": str(first), "sample_end": str(last), "existing_aggregates": count, "incident_engine": event_engine, "apply": apply}), flush=True)
        if not apply:
            return

        # 先保存真实原表结构与全部衍生数据；不会包含账号或通知渠道凭据。
        backup = Path("logs") / ("data-repair-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + ".jsonl")
        backup.parent.mkdir(exist_ok=True)
        with backup.open("x", encoding="utf-8") as output:
            async with engine.connect() as conn:
                for table in ("alert_incidents", "aggregates"):
                    ddl = (await conn.execute(text(f"SHOW CREATE TABLE {table}"))).one()[1]
                    output.write(json.dumps({"table": table, "ddl": ddl}, ensure_ascii=False) + "\n")
                    result = await conn.stream(text(f"SELECT * FROM {table}"))
                    async for row in result.mappings():
                        output.write(json.dumps({"table": table, "row": dict(row)}, default=str, ensure_ascii=False) + "\n")
        print("backup=" + str(backup.resolve()), flush=True)
        if event_engine != "InnoDB":
            async with engine.begin() as conn:
                await conn.execute(text("ALTER TABLE alert_incidents ENGINE=InnoDB"))
        if first is None:
            return
        origin = datetime(1970, 1, 1)
        # 首窗口可能从留存截断处开始，跳过该不完整窗口。
        start = origin + timedelta(seconds=((int((first-origin).total_seconds())+299)//300)*300)
        end = origin + timedelta(seconds=(int((min(last + timedelta(minutes=1), utcnow())-origin).total_seconds())//300)*300)
        repo = MySQLRepository(use_retry=False)
        cursor = start
        total = 0
        while cursor < end:
            batch_end = min(cursor + timedelta(hours=6), end)
            result = await repo.rollup_samples_to_aggregates(lookback_minutes=int((batch_end-cursor).total_seconds()//60), end_at=batch_end)
            if "error" in result:
                raise RuntimeError("聚合批次失败，备份已保留；修复后可幂等重跑")
            total += result["written"]
            cursor = batch_end
            print(json.dumps({"through_utc": str(cursor), "windows_written": total}), flush=True)
        async with engine.connect() as conn:
            invalid = (await conn.execute(text("SELECT COUNT(*) FROM aggregates WHERE latency_p50_ms > latency_p95_ms OR latency_p95_ms > latency_p99_ms OR latency_p50_ms < latency_min_ms - 0.01 OR latency_p99_ms > latency_max_ms + 0.01"))).scalar_one()
        if invalid:
            raise RuntimeError(f"聚合不变量校验失败: {invalid} 行")
        print(json.dumps({"complete": True, "windows_written": total, "invalid_percentiles": invalid}), flush=True)
    finally:
        await close_db()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    asyncio.run(run(args.apply))
