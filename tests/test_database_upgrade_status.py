"""Upgrade diagnosis is read-only and never exposes raw SQL/credentials."""
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from scripts.database_upgrade_status import inspect_upgrade


class Results:
    def __init__(self, scalar=None, rows=()):
        self.value = scalar
        self.rows = rows
    def scalar(self):
        return self.value
    def mappings(self):
        return self
    def all(self):
        return self.rows


class Database:
    def __init__(self, owner=None, engine="InnoDB", process=None, denied=False):
        self.owner=owner
        self.engine=engine
        self.process=process
        self.denied=denied
        self.statements=[]
    @asynccontextmanager
    async def connect(self):
        yield self
    async def execute(self, statement, parameters=None):
        sql=str(statement)
        self.statements.append(sql)
        assert sql.lstrip().startswith("SELECT"), sql
        if "SELECT DATABASE()" in sql:
            return Results(scalar="isolated_test")
        if "IS_USED_LOCK" in sql:
            return Results(scalar=self.owner)
        if "information_schema.tables" in sql:
            return Results(rows=[{"TABLE_NAME":"samples","ENGINE":self.engine,
                                 "TABLE_ROWS":12345,"DATA_LENGTH":1048576,"INDEX_LENGTH":0}])
        if self.denied:
            raise PermissionError("internal credential secret")
        return Results(rows=[self.process] if self.process else [])


async def test_conversion_report_uses_safe_fields_and_selects_only():
    database=Database(owner=17,engine="MyISAM",process={
        "ID":17,"COMMAND":"Query","TIME":83,"STATE":"copy to tmp table",
        "INFO":"ALTER TABLE `samples` ENGINE=InnoDB /* confidential SQL details */",
    })
    report=await inspect_upgrade(database)
    assert report["upgrade_lock_owner"]==17
    assert report["processes"][0]["engine_conversion"]
    assert report["processes"][0]["table"]=="samples"
    assert report["processes"][0]["elapsed_seconds"]==83
    assert "confidential" not in str(report)
    assert "等待完成" in report["advice"]
    assert report["tables"][0]["size_mb"]==1.0


@pytest.mark.parametrize("engine,expected",[("InnoDB","可手动启动"),("MyISAM","仅运行一次")])
async def test_no_owner_gives_appropriate_action(engine,expected):
    report=await inspect_upgrade(Database(engine=engine))
    assert expected in report["advice"]


async def test_limited_process_visibility_does_not_claim_lock_is_free():
    report=await inspect_upgrade(Database(owner=99,engine="MyISAM",denied=True))
    assert report["upgrade_lock_owner"]==99
    assert report["process_visibility"]=="unavailable_or_permission_limited"
    assert "暂不重复运行迁移" in report["advice"]
    assert "internal credential" not in str(report)
