"""修复可由原始成功样本证明的旧事件本地时间偏移；默认只预览。"""
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


def corrected_resolution(row, offset_hours):
    first_success = row["first_success"]
    if first_success is None or row["resolved_at"] is None:
        return None
    corrected = row["resolved_at"] - timedelta(hours=offset_hours)
    if corrected < row["started_at"] or abs((corrected-first_success).total_seconds()) > 60:
        return None
    if row["duration_seconds"] != int((row["resolved_at"]-row["started_at"]).total_seconds()):
        return None
    return corrected


async def run(offset_hours, apply):
    c=get_config().database
    engine=await init_db(host=c.mysql_host,port=c.mysql_port,user=c.mysql_user,password=c.mysql_password,database=c.mysql_database,create_tables=False)
    try:
        async with engine.connect() as conn:
            rows=(await conn.execute(text("SELECT i.*, (SELECT MIN(s.timestamp) FROM samples s WHERE s.node_name=i.node_name AND s.success=1 AND s.timestamp >= i.started_at AND s.timestamp <= i.resolved_at) AS first_success FROM alert_incidents i WHERE i.status='resolved'"))).mappings().all()
        plan=[]
        for row in rows:
            corrected=corrected_resolution(row,offset_hours)
            if corrected is not None:
                plan.append({"before":dict(row),"resolved_at":corrected,"duration_seconds":int((corrected-row["started_at"]).total_seconds())})
        print(json.dumps({"candidates":[{"id":r["before"]["id"],"old_duration":r["before"]["duration_seconds"],"new_duration":r["duration_seconds"]} for r in plan],"apply":apply}),flush=True)
        if not apply or not plan:
            return
        async with engine.connect() as conn:
            storage_engine=(await conn.execute(text("SELECT ENGINE FROM information_schema.tables WHERE table_schema=DATABASE() AND table_name='alert_incidents'"))).scalar_one()
            if storage_engine != "InnoDB":
                raise RuntimeError("请先执行 bootstrap_database.py 转换事件表为 InnoDB")
        backup=Path("logs")/("incident-time-repair-"+datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")+".json")
        backup.parent.mkdir(exist_ok=True)
        with backup.open("x",encoding="utf-8") as f:
            json.dump(plan,f,default=str,ensure_ascii=False,indent=2)
        async with engine.begin() as conn:
            for row in plan:
                old=row["before"]
                result=await conn.execute(text("UPDATE alert_incidents SET resolved_at=:new_resolved, duration_seconds=:new_duration WHERE id=:id AND status='resolved' AND resolved_at=:old_resolved AND duration_seconds=:old_duration"),dict(new_resolved=row["resolved_at"],new_duration=row["duration_seconds"],id=old["id"],old_resolved=old["resolved_at"],old_duration=old["duration_seconds"]))
                if result.rowcount != 1:
                    raise RuntimeError("事件已变化，本批次回滚")
        print(json.dumps({"repaired":len(plan),"backup":str(backup.resolve())}),flush=True)
    finally:
        await close_db()


if __name__ == "__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--legacy-offset-hours",type=int,required=True,choices=range(-14,15))
    parser.add_argument("--apply",action="store_true")
    args=parser.parse_args()
    if args.legacy_offset_hours == 0:
        parser.error("offset must be nonzero")
    asyncio.run(run(args.legacy_offset_hours,args.apply))
