"""
SQL dump 导入工具

用途：
    把 Navicat / mysqldump 导出的 .sql 文件导入当前配置的数据库。
    采用流式逐行读取，避免把 900MB+ 的文件整体读进内存。

特性：
    - 边读边执行，单条 INSERT 最大可达数 MB，不受内存限制
    - 自动跳过纯注释行
    - 执行 SET FOREIGN_KEY_CHECKS=0 等会话级语句
    - 导入后可选转换为 InnoDB（dump 常为 MyISAM，而应用依赖事务）

用法：
    python scripts/import_sql.py <dump.sql>
    python scripts/import_sql.py <dump.sql> --to-innodb
"""

import argparse
import asyncio
import re
import sys
import time
from pathlib import Path

# 确保项目根目录可导入
ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from sqlalchemy import text

from app.models.base import get_engine
from app.services.config import load_config

# 跳过：纯注释与不需要执行的语句
SKIP_PREFIXES = (
    "-- ",
    "--\n",
    "/*!40014 SET @OLD_",       # 会话变量保存，由末尾的还原语句处理
    "/*!40101 SET @OLD_",
    "/*!40103 SET @OLD_",
    "/*!40111 SET @OLD_",
    "/*!40101 SET NAMES",
    "/*!40103 SET TIME_ZONE",
    "/*!40014 SET @OLD_SQL_MODE",
)

# 表名提取（用于转换引擎）
TABLE_RE = re.compile(r"CREATE TABLE `([^`]+)`")


def is_skippable(line: str) -> bool:
    """判断是否应跳过的行"""
    s = line.lstrip()
    if not s:
        return True
    return s.startswith(SKIP_PREFIXES)


async def import_dump(path: Path, engine) -> dict:
    """流式导入 SQL 文件"""
    total_bytes = path.stat().st_size
    read_bytes = 0
    statements = 0
    inserted = 0
    tables: list[str] = []

    started = time.perf_counter()
    buffer = ""

    async with engine.begin() as conn:
        # 会话级设置：关闭外键检查以便顺序导入
        await conn.execute(text("SET FOREIGN_KEY_CHECKS=0"))
        await conn.execute(text("SET UNIQUE_CHECKS=0"))

        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                read_bytes += len(line.encode("utf-8", errors="replace"))

                if not buffer.strip():
                    if is_skippable(line):
                        continue
                    buffer = line.strip()

                else:
                    buffer += "\n" + line.strip()

                # 语句以分号结尾才算完成
                if not buffer.endswith(";"):
                    continue

                stmt = buffer.strip()
                buffer = ""

                if is_skippable(stmt):
                    continue

                # 记录表名
                m = TABLE_RE.match(stmt)
                if m:
                    tables.append(m.group(1))

                upper = stmt.upper()
                try:
                    if upper.startswith("INSERT"):
                        result = await conn.execute(text(stmt))
                        inserted += max(result.rowcount or 0, 0)
                    elif upper.startswith(("CREATE", "DROP", "ALTER", "SET", "LOCK", "UNLOCK")):
                        await conn.execute(text(stmt))
                    else:
                        await conn.execute(text(stmt))
                    statements += 1
                except Exception as e:
                    # 单条失败不中断整个导入，但必须报告
                    head = stmt[:110].replace("\n", " ")
                    print(f"  [失败] {type(e).__name__}: {str(e)[:110]}")
                    print(f"         语句开头: {head}")
                    statements += 1

                # 进度：每 50MB 或每 200 条语句报告一次
                if read_bytes - (read_bytes // (50 * 1024 * 1024)) * (50 * 1024 * 1024) < 1024 * 1024:
                    pct = read_bytes * 100 / total_bytes
                    print(f"  进度 {pct:5.1f}%  已读 {read_bytes / 1024 / 1024:.0f}MB"
                          f"  语句 {statements}  行 {inserted}",
                          flush=True)

        # 文件末尾若残留未闭合内容
        if buffer.strip() and not buffer.strip().endswith(";"):
            try:
                await conn.execute(text(buffer.strip()))
                statements += 1
            except Exception as e:
                print(f"  [尾部语句失败] {type(e).__name__}: {str(e)[:110]}")

        await conn.execute(text("SET FOREIGN_KEY_CHECKS=1"))

    elapsed = time.perf_counter() - started
    return {
        "tables": tables,
        "statements": statements,
        "inserted": inserted,
        "seconds": elapsed,
        "mb": total_bytes / 1024 / 1024,
    }


async def convert_to_innodb(engine, tables: list[str]) -> list[str]:
    """
    把表转换为 InnoDB

    mysqldump 常用 MyISAM，而 SQLAlchemy 的会话依赖事务。
    MyISAM 忽略事务，异常时无法回滚，��能保证数据一致性。
    """
    converted = []
    async with engine.begin() as conn:
        for t in tables:
            try:
                await conn.execute(text(f"ALTER TABLE `{t}` ENGINE=InnoDB"))
                converted.append(t)
                print(f"  [OK] {t} -> InnoDB")
            except Exception as e:
                print(f"  [失败] {t}: {type(e).__name__}: {str(e)[:90]}")
    return converted


async def show_counts(engine, tables: list[str]) -> None:
    """输出各表行数"""
    print()
    print("  导入后各表行数：")
    async with engine.connect() as conn:
        result = await conn.execute(text("SHOW TABLES"))
        existing = [r[0] for r in result.fetchall()]
        for t in sorted(existing):
            try:
                r = await conn.execute(text(f"SELECT COUNT(*) FROM `{t}`"))
                print(f"    {t:26s} {r.scalar():>10,} 行")
            except Exception as e:
                print(f"    {t:26s} 查询失败: {str(e)[:50]}")


async def main() -> int:
    parser = argparse.ArgumentParser(description="导入 SQL dump 到当前数据库")
    parser.add_argument("dump", help="SQL 文件路径")
    parser.add_argument(
        "--to-innodb", action="store_true",
        help="导入后把表转换为 InnoDB（应用依赖事务）",
    )
    args = parser.parse_args()

    path = Path(args.dump)
    if not path.is_file():
        # 兼容传入目录的情况
        candidates = list(path.glob("*.sql")) if path.is_dir() else []
        if not candidates:
            print(f"[错误] 找不到 SQL 文件: {args.dump}")
            return 1
        path = candidates[0]
        print(f"[信息] 使用目录中的文件: {path.name}")

    size_mb = path.stat().st_size / 1024 / 1024
    print("=" * 62)
    print("SQL 导入")
    print("=" * 62)
    print(f"  文件: {path.name}")
    print(f"  大小: {size_mb:.1f} MB")

    cfg = load_config()
    print(f"  目标: {cfg.database.mysql_host}:{cfg.database.mysql_port}"
          f"/{cfg.database.mysql_database}")
    print()
    print("  注意：dump 中的 DROP TABLE 会删除同名旧表后重建。")
    print()

    engine = get_engine(
        host=cfg.database.mysql_host,
        port=cfg.database.mysql_port,
        user=cfg.database.mysql_user,
        password=cfg.database.mysql_password,
        database=cfg.database.mysql_database,
    )

    try:
        print("[1/3] 导入中 ...")
        result = await import_dump(path, engine)
        print(f"  完成：{result['statements']} 条语句，"
              f"约 {result['inserted']:,} 行，耗时 {result['seconds']:.1f}s")
        print(f"  表: {', '.join(result['tables'])}")

        if args.to_innodb:
            print()
            print("[2/3] 转换为 InnoDB ...")
            await convert_to_innodb(engine, result["tables"])
        else:
            print()
            print("[2/3] 跳过引擎转换（如需请加 --to-innodb）")

        print()
        print("[3/3] 校验 ...")
        await show_counts(engine, result["tables"])
    except Exception as e:
        print(f"  [失败] {type(e).__name__}: {str(e)[:200]}")
        return 1
    finally:
        await engine.dispose()

    print()
    print("=" * 62)
    print("导入完成。")
    print()
    print("后续步骤：")
    print("  1. 重跑引导脚本，补上 dump 中没有的新表与新列：")
    print("     python scripts/bootstrap_database.py")
    print("=" * 62)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))