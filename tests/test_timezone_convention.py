"""
时间序列化与时区约定测试

背景：项目的时间约定历史上不统一，且序列化时是否带 Z 后缀
直接决定前端显示是否正确。

    前端 formatTime() 用 new Date(isoStr) 解析：
      - 带 Z        -> 识别为 UTC，换算成本地时间显示
      - 不带 Z      -> 按本地时间解析，原样显示

    因此"数据库里存的是什么时区"与"序列化时加不加 Z"必须匹配，
    否则显示会偏差一个时区（本地为 UTC+8 时即 8 小时）。

各表现状：
    samples.timestamp         UTC    序列化为 datetime 对象，趋势图显式加 "Z"
    alert_logs.triggered_at  本地    datetime.now()，序列化不加 Z
    notification_logs.sent_at 本地   func.now()（MySQL 系统时区），不加 Z
    alert_incidents.*         UTC    来自心跳时间戳，必须加 Z
"""

from datetime import datetime

import pytest


class TestIncidentSerialization:
    """事件表时间字段必须带 Z"""

    @pytest.fixture
    def incident(self):
        from app.models.notification_channel import AlertIncidentModel

        return AlertIncidentModel(
            id=1,
            node_name="test-node",
            alert_type="node_down",
            started_at=datetime(2026, 10, 8, 0, 53, 11),
            last_notified_at=datetime(2026, 10, 8, 0, 53, 11),
            next_notify_at=datetime(2026, 10, 8, 1, 53, 11),
            resolved_at=None,
        )

    def test_started_at_has_z_suffix(self, incident):
        """事件开始时间来自心跳（UTC），必须标记为 UTC"""
        d = incident.to_dict()
        assert d["started_at"].endswith("Z"), (
            f"started_at 缺少 Z 后缀，前端会按本地解析导致显示早 8 小时：{d['started_at']}"
        )

    def test_next_notify_at_has_z_suffix(self, incident):
        d = incident.to_dict()
        assert d["next_notify_at"].endswith("Z")

    def test_last_notified_at_has_z_suffix(self, incident):
        d = incident.to_dict()
        assert d["last_notified_at"].endswith("Z")

    def test_resolved_at_has_z_suffix(self):
        from app.models.notification_channel import AlertIncidentModel

        m = AlertIncidentModel(
            id=1, node_name="n", alert_type="node_down",
            started_at=datetime(2026, 10, 8, 0, 0, 0),
            resolved_at=datetime(2026, 10, 8, 8, 0, 0),
        )
        assert m.to_dict()["resolved_at"].endswith("Z")

    def test_none_fields_stay_none(self, incident):
        """未设置的字段不应变成字符串"""
        d = incident.to_dict()
        assert d["resolved_at"] is None
        assert d["created_at"] is None


class TestLocalTimeTablesKeepNoSuffix:
    """本地时间表不能加 Z"""

    def test_alert_log_triggered_at_has_no_z(self):
        """
        alert_logs 存本地时间，加 Z 会多偏 8 小时

        该字段由 datetime.now() 写入（本地），不是 UTC。
        """
        from app.models.notification_channel import AlertLogModel

        m = AlertLogModel(
            id=1,
            node_name="n",
            alert_type="node_down",
            triggered_at=datetime(2026, 10, 8, 8, 53, 20),
        )
        d = m.to_dict()
        assert not d["triggered_at"].endswith("Z"), (
            f"triggered_at 是本地时间，不应带 Z：{d['triggered_at']}"
        )

    def test_notification_log_sent_at_has_no_z(self):
        """
        notification_logs 由 MySQL func.now() 写入

        取的是 MySQL 会话时区（通常是系统时区），不是 UTC。
        """
        from app.models.notification_channel import NotificationLogModel

        m = NotificationLogModel(
            id=1,
            channel_id=1,
            channel_name="c",
            alert_type="node_down",
            sent_at=datetime(2026, 10, 8, 8, 53, 20),
        )
        d = m.to_dict()
        assert not d["sent_at"].endswith("Z"), (
            f"sent_at 是 MySQL 本地时间，不应带 Z：{d['sent_at']}"
        )


class TestTimeUtilHelpers:
    """timeutil 的格式转换"""

    def test_format_iso_utc_appends_z(self):
        from app.utils.timeutil import format_iso_utc

        out = format_iso_utc(datetime(2026, 10, 8, 0, 53, 11))
        assert out == "2026-10-08T00:53:11Z"

    def test_format_iso_utc_converts_aware(self):
        from app.utils.timeutil import format_iso_utc

        ts = datetime(2026, 10, 8, 8, 53, 11, tzinfo=__import__("datetime").timezone.utc)
        assert format_iso_utc(ts).endswith("Z")

    def test_format_iso_utc_none(self):
        from app.utils.timeutil import format_iso_utc

        assert format_iso_utc(None) is None


class TestStorageConventionDocumented:
    """约定需与文档一致，防止后续误改"""

    def test_alert_log_uses_local_now(self):
        """
        alert_logs 用 datetime.now()（本地）

        若将来改成 UTC，必须同步去掉 to_dict 里的 Z 后缀 ——
        否则显示会偏 8 小时。这个测试锁住当前约定。
        """
        from pathlib import Path

        src = (
            Path(__file__).parent.parent
            / "app" / "services" / "alerting.py"
        ).read_text(encoding="utf-8")
        assert "triggered_at=datetime.now()" in src, (
            "alert_logs 的时间来源变了 —— 请同步检查序列化是否需要 Z 后缀"
        )

    def test_notification_log_uses_func_now(self):
        """notification_logs 依赖 MySQL 默认，因此不能加 Z"""
        from pathlib import Path

        src = (
            Path(__file__).parent.parent
            / "app" / "models" / "notification_channel.py"
        ).read_text(encoding="utf-8")
        assert "default=func.now()" in src, (
            "notification_logs.sent_at 的写入方式变了 —— "
            "请确认序列化是否仍应省略 Z"
        )

    def test_readme_documents_convention(self):
        """README 需说明时间约定"""
        from pathlib import Path

        readme = (
            Path(__file__).parent.parent / "README.md"
        ).read_text(encoding="utf-8")
        assert "时间约定" in readme
        # 需说明序列化与存储时区的关系
        assert "UTC" in readme