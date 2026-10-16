"""大表首次迁移不能被旧的健康检查/迁移时限过早打断。"""
from pathlib import Path
from unittest.mock import Mock

import pytest

from app.services.update_runtime import UpdateError
from scripts.run_server import ServerManager
from test_updates import project


@pytest.fixture
def manager(tmp_path, monkeypatch):
    manager = ServerManager(project(tmp_path / "installed"))
    monkeypatch.setattr(manager, "environment", lambda: {})
    return manager


def test_migration_budget_defaults_to_one_hour(manager, tmp_path, monkeypatch):
    staged = project(tmp_path / "staged")
    command = Mock()
    monkeypatch.setattr(manager, "command", command)
    manager.migrate(staged, "test-python")
    command.assert_called_once_with(
        ["test-python", str(staged / "scripts/bootstrap_database.py")],
        3600, "检查数据库结构",
    )


def test_configured_migration_and_health_waits_use_project_environment(manager, monkeypatch):
    monkeypatch.setattr(manager, "environment", lambda: {
        "MONITOR_UPDATE_MIGRATION_TIMEOUT_SECONDS": "7200",
        "MONITOR_UPDATE_HEALTH_TIMEOUT_SECONDS": "2400",
    })
    assert manager._waiting_timeout("MONITOR_UPDATE_MIGRATION_TIMEOUT_SECONDS", 3600, 60) == 7200
    assert manager._waiting_timeout("MONITOR_UPDATE_HEALTH_TIMEOUT_SECONDS", 1800, 30) == 2400


@pytest.mark.parametrize("value", ["bad", "0", "29", "86401", None])
def test_invalid_wait_budget_is_actionable(manager, monkeypatch, value):
    monkeypatch.setattr(manager, "environment", lambda: {"MONITOR_UPDATE_HEALTH_TIMEOUT_SECONDS": value})
    with pytest.raises(UpdateError, match="MONITOR_UPDATE_HEALTH_TIMEOUT_SECONDS"):
        manager._waiting_timeout("MONITOR_UPDATE_HEALTH_TIMEOUT_SECONDS", 1800, 30)


def test_default_health_budget_is_passed_to_the_existing_probe_loop(manager, monkeypatch):
    reads=[]
    def budget(variable, default, minimum):
        reads.append((variable, default, minimum))
        return 0
    monkeypatch.setattr(manager, "_waiting_timeout", budget)
    assert manager.healthy("1.0.0") is False
    assert reads == [("MONITOR_UPDATE_HEALTH_TIMEOUT_SECONDS", 1800, 30)]
