"""
告警配置写回测试

面板上的告警设置修改的是内存对象。若不落盘，服务重启后回到旧值，
用户会以为"没保存成功"；而早期的实现用 yaml.safe_dump 重写整个文件，
会把配置里的注释全部抹掉（实测丢失 21 行）。

所以这里锁定两件事：改动确实落盘、注释与无关字段完好。
所有测试都在临时副本上进行，不触碰真实的 configs/app.yaml。
"""

import shutil
from pathlib import Path

import pytest
import yaml

from app.services import config as config_mod
from app.services.config import load_config, save_alerting_config

REAL_CONFIG = Path(__file__).parent.parent / "configs" / "app.yaml"
NODES_CONFIG = Path(__file__).parent.parent / "configs" / "nodes.yaml"


@pytest.fixture
def config_copy(tmp_path):
    """把真实配置复制到临时目录，返回 (副本路径, 原始文本)"""
    target = tmp_path / "app.yaml"
    shutil.copy(REAL_CONFIG, target)
    return target, target.read_text(encoding="utf-8")


def count_comments(text: str) -> int:
    return sum(1 for line in text.splitlines() if line.strip().startswith("#"))


class TestSaveAlertingConfig:
    """写回 app.yaml"""

    def test_values_are_persisted(self, config_copy):
        path, _ = config_copy
        load_config(app_config_path=str(path), nodes_config_path=str(NODES_CONFIG))
        cfg = config_mod.get_config()

        cfg.alerting.reminder_intervals = (0, 30, 120, 360)
        cfg.alerting.intervals_loop_minutes = 600
        cfg.alerting.quiet_hours_enabled = True
        cfg.alerting.quiet_hours_start = "22:30"
        cfg.alerting.quiet_hours_end = "06:30"
        cfg.alerting.max_notifications_per_incident = 8
        cfg.alerting.consecutive_failures = 5

        assert save_alerting_config(str(path)) is True

        saved = yaml.safe_load(path.read_text(encoding="utf-8"))["alerting"]
        assert saved["reminder_intervals_minutes"] == [0, 30, 120, 360]
        assert saved["intervals_loop_minutes"] == 600
        assert saved["max_notifications_per_incident"] == 8
        assert saved["quiet_hours"] == {
            "enabled": True, "start": "22:30", "end": "06:30",
        }

    def test_values_survive_reload(self, config_copy):
        """重新加载能读回新值 —— 这才是"保存成功"的真正含义"""
        path, _ = config_copy
        load_config(app_config_path=str(path), nodes_config_path=str(NODES_CONFIG))
        config_mod.get_config().alerting.reminder_intervals = (0, 15, 45)
        save_alerting_config(str(path))

        reloaded = load_config(
            app_config_path=str(path), nodes_config_path=str(NODES_CONFIG)
        )
        assert reloaded.alerting.reminder_intervals == (0, 15, 45)

    def test_comments_are_preserved(self, config_copy):
        """
        注释必须完整保留

        段内注释是运维理解各项含义的唯一线索，早期实现会把它们全部抹掉。
        """
        path, original = config_copy
        load_config(app_config_path=str(path), nodes_config_path=str(NODES_CONFIG))
        config_mod.get_config().alerting.quiet_hours_enabled = True
        save_alerting_config(str(path))

        after = path.read_text(encoding="utf-8")
        assert count_comments(after) == count_comments(original), (
            "写回过程丢失了注释"
        )

    def test_unmanaged_fields_preserved(self, config_copy):
        """channels 等面板未管理的子项不能被抹掉"""
        path, _ = config_copy
        load_config(app_config_path=str(path), nodes_config_path=str(NODES_CONFIG))
        config_mod.get_config().alerting.reminder_intervals = (0, 60)
        save_alerting_config(str(path))

        saved = yaml.safe_load(path.read_text(encoding="utf-8"))["alerting"]
        assert "channels" in saved
        assert "webhook" in saved["channels"]

    def test_other_sections_untouched(self, config_copy):
        """database / scheduler / logging / server 段落不受影响"""
        path, _ = config_copy
        before = yaml.safe_load(path.read_text(encoding="utf-8"))
        load_config(app_config_path=str(path), nodes_config_path=str(NODES_CONFIG))
        config_mod.get_config().alerting.enabled = False
        save_alerting_config(str(path))

        after = yaml.safe_load(path.read_text(encoding="utf-8"))
        for section in ("database", "scheduler", "logging", "server", "app"):
            assert after[section] == before[section], f"{section} 段被改动"

    def test_output_is_valid_yaml(self, config_copy):
        path, _ = config_copy
        load_config(app_config_path=str(path), nodes_config_path=str(NODES_CONFIG))
        save_alerting_config(str(path))
        # 解析不抛异常即通过
        assert isinstance(yaml.safe_load(path.read_text(encoding="utf-8")), dict)

    def test_time_values_quoted_to_stay_strings(self, config_copy):
        """
        时间值必须加引号

        YAML 会把裸写的 23:00 当成六进制整数解析成 1380，
        静默时段配置因此静默失效。
        """
        path, _ = config_copy
        load_config(app_config_path=str(path), nodes_config_path=str(NODES_CONFIG))
        cfg = config_mod.get_config()
        cfg.alerting.quiet_hours_enabled = True
        cfg.alerting.quiet_hours_start = "23:00"
        cfg.alerting.quiet_hours_end = "07:00"
        save_alerting_config(str(path))

        saved = yaml.safe_load(path.read_text(encoding="utf-8"))["alerting"]
        assert saved["quiet_hours"]["start"] == "23:00"
        assert isinstance(saved["quiet_hours"]["start"], str)

    def test_missing_file_returns_false(self, tmp_path):
        load_config(app_config_path=str(REAL_CONFIG), nodes_config_path=str(NODES_CONFIG))
        assert save_alerting_config(str(tmp_path / "nope.yaml")) is False

    def test_real_config_untouched_by_tests(self, config_copy):
        """夹具用的是副本，真实配置文件不应被这些测试改动"""
        before = REAL_CONFIG.read_bytes()
        path, _ = config_copy
        load_config(app_config_path=str(path), nodes_config_path=str(NODES_CONFIG))
        config_mod.get_config().alerting.enabled = False
        save_alerting_config(str(path))
        assert REAL_CONFIG.read_bytes() == before
