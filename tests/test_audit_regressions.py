"""审计回归：使用真实 ORM/SQL 和业务链，仅替换网络与数据库驱动边界。"""

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.models.base import Base
from app.models.notification_channel import AlertIncidentModel, NotificationLogModel
from app.services.config import AlertingConfig
from app.services.alerting import AlertingService, NodeAlertState, AlertEvent, process_alerts
from app.services.heartbeat import HeartbeatResult
from app.services.incident import IncidentService


@pytest.fixture
def sql_store(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)

    class AsyncSessionAdapter:
        def __init__(self, session):
            self.session = session

        async def execute(self, statement):
            return self.session.execute(statement)

        def add(self, obj):
            self.session.add(obj)

        async def commit(self):
            self.session.commit()

        async def flush(self):
            self.session.flush()

        async def refresh(self, obj):
            self.session.refresh(obj)

        async def rollback(self):
            self.session.rollback()

    @asynccontextmanager
    async def context():
        with Session(engine, expire_on_commit=False) as session:
            try:
                yield AsyncSessionAdapter(session)
                session.commit()
            except Exception:
                session.rollback()
                raise

    for module in ("incident", "alerting", "notification"):
        monkeypatch.setattr(f"app.services.{module}.get_session_context", context)
    yield engine, context
    engine.dispose()


def heartbeat(success=True, timestamp=None, name="audit-node"):
    return HeartbeatResult(node_name=name, url="http://audit.invalid", timestamp=timestamp or datetime(2026, 10, 12, 0, 5), status_code=200 if success else 0, latency_ms=10, success=success)


async def seed_incident(delivered=False):
    incident = await IncidentService.open_incident("audit-node", "node_down", "http://audit.invalid", datetime(2026, 10, 12))
    if delivered:
        from app.services.notification import NotificationLogService
        await NotificationLogService.add_log(1, "test", "node_down", True, incident_id=incident["id"], notify_seq=1)
    return incident


async def test_real_serialization_roundtrip_gate_and_duration(sql_store):
    incident = await seed_incident()
    updated = await IncidentService.record_notification(incident["id"], (0, 60, 240), 720, now=datetime(2026, 10, 12))
    assert await IncidentService.should_notify(updated, datetime(2026, 10, 12, 0, 5)) == (False, "闸门未到，还需 3300 秒")
    assert (await IncidentService.should_notify(updated, datetime(2026, 10, 12, 1, tzinfo=timezone.utc)))[0]
    assert AlertingService._format_duration(updated["started_at"], datetime(2026, 10, 12, 0, 5)) == "5分钟"
    closed = await IncidentService.resolve_incident(incident["id"], datetime(2026, 10, 12, 0, 5))
    assert closed["duration_seconds"] == 300
    assert closed["resolved_at"].endswith("Z")


@pytest.mark.parametrize("delivered,suppressed,expected_notifications", [(True, False, 1), (False, False, 0), (True, True, 0)])
async def test_recovery_after_restart_always_closes_event(sql_store, delivered, suppressed, expected_notifications):
    incident = await seed_incident(delivered)
    svc = AlertingService(AlertingConfig(suppress_persist=False))
    if suppressed:
        svc._states["audit-node"] = NodeAlertState("audit-node", consecutive_failures=3, last_success=False, suppressed_alerts={"node_recovery": datetime.now()})
    events = await svc.evaluate(heartbeat())
    assert len(events) == expected_notifications
    assert await IncidentService.get_open_incident("audit-node", "node_down") is None
    with Session(sql_store[0]) as session:
        assert session.get(AlertIncidentModel, incident["id"]).duration_seconds == 300
    assert await svc.evaluate(heartbeat()) == []


async def test_one_bad_node_does_not_drop_other_alerts(monkeypatch):
    svc = AlertingService(AlertingConfig(suppress_persist=False))
    event = AlertEvent("node_down", "other", "http://audit.invalid", "down")
    monkeypatch.setattr(svc, "evaluate", AsyncMock(side_effect=[ValueError("invalid incident"), [event]]))
    assert await svc.evaluate_batch([heartbeat(), heartbeat(name="other")]) == [event]


@pytest.mark.parametrize("success", [True, False])
async def test_send_pipeline_persists_incident_association(sql_store, monkeypatch, success):
    from app.services.notification import NotificationService, EmailNotificationService, NotificationRecipientService
    from app.services.alerting import AlertLogService
    incident = await seed_incident()
    channel = dict(id=1, name="test", channel_type="email", enabled=True, notify_on_down=True, smtp_host="test.invalid", smtp_port=465, smtp_user="test", smtp_password="fixture-only", smtp_ssl=True)
    monkeypatch.setattr(NotificationService, "get_enabled_channels", AsyncMock(return_value=[channel]))
    monkeypatch.setattr(NotificationService, "get_channel_by_id", AsyncMock(return_value=channel))
    monkeypatch.setattr(NotificationService, "check_send_limits", AsyncMock(return_value=(True, "")))
    monkeypatch.setattr(NotificationService, "update_channel_status", AsyncMock())
    monkeypatch.setattr(NotificationRecipientService, "get_enabled_emails", AsyncMock(return_value=["test@example.invalid"]))
    sender = AsyncMock(return_value=(success, "" if success else "offline"))
    monkeypatch.setattr(EmailNotificationService, "send_email", sender)
    monkeypatch.setattr(AlertLogService, "update_notification_status", AsyncMock())
    event = AlertEvent("node_down", "audit-node", "http://audit.invalid", "down", incident_id=incident["id"], notify_seq=2)
    await process_alerts([event])
    sender.assert_awaited_once()
    with Session(sql_store[0]) as session:
        log = session.execute(select(NotificationLogModel)).scalar_one()
        assert (log.incident_id, log.notify_seq, log.success) == (incident["id"], 2, success)
    assert await IncidentService.has_delivered_down_alert(incident["id"]) is success


@pytest.mark.parametrize("operation", ["create", "update", "delete"])
async def test_failed_node_mutation_does_not_change_cache(monkeypatch, operation):
    from app.routes import nodes
    from app.schemas.node import NodeCreate, NodeUpdate, NodeStatus
    cached = NodeStatus(name="audit-node", url="http://original.invalid", enabled=True, tags=[], region="default")
    monkeypatch.setattr(nodes, "_node_status_cache", {cached.name: cached})
    row = None if operation == "create" else cached.model_dump()
    repo = SimpleNamespace(get_node_by_name=AsyncMock(return_value=row), create_node=AsyncMock(return_value=None), update_node=AsyncMock(return_value=False), delete_node=AsyncMock(return_value=False))
    monkeypatch.setattr(nodes, "_get_db_repository", lambda: repo)
    with pytest.raises(HTTPException) as error:
        if operation == "create":
            await nodes.create_node(NodeCreate(name="audit-new", url="http://new.invalid"))
        elif operation == "update":
            await nodes.update_node(cached.name, NodeUpdate(url="http://new.invalid"))
        else:
            await nodes.delete_node(cached.name, hard=True)
    assert error.value.status_code == 503
    assert nodes._node_status_cache == {cached.name: cached}
    assert cached.url == "http://original.invalid"


@pytest.mark.parametrize("save_success,invalid", [(True, True), (False, False)])
async def test_config_failure_is_atomic(monkeypatch, save_success, invalid):
    from app.routes.notifications import update_alert_config, AlertConfigUpdate
    from app.services import config
    cfg = SimpleNamespace(alerting=AlertingConfig(enabled=True))
    monkeypatch.setattr(config, "get_config", lambda: cfg)
    monkeypatch.setattr(config, "save_alerting_config", lambda **kwargs: save_success)
    with pytest.raises(HTTPException) as error:
        await update_alert_config(AlertConfigUpdate(enabled=False, consecutive_failures=0 if invalid else 3))
    assert error.value.status_code == (400 if invalid else 503)
    assert cfg.alerting.enabled is True


def test_recovery_and_escalation_config_survive_reload(tmp_path):
    from app.services.config import load_config, save_alerting_config
    path = tmp_path / "app.yaml"
    path.write_text("alerting:\n  enabled: true\n", encoding="utf-8")
    candidate = AlertingConfig(notify_on_recovery=False, escalate_after_minutes=60)
    assert save_alerting_config(str(path), alerting=candidate)
    reloaded = load_config(str(path), str(tmp_path / "nodes.yaml"))
    assert reloaded.alerting.notify_on_recovery is False
    assert reloaded.alerting.escalate_after_minutes == 60


async def test_delayed_down_notification_cancelled_after_recovery(sql_store, monkeypatch):
    from app.services.notification import NotificationService, EmailNotificationService, NotificationRecipientService
    incident = await seed_incident()
    await IncidentService.resolve_incident(incident["id"], datetime(2026, 10, 12, 0, 5))
    channel = dict(id=1, name="test", channel_type="email", enabled=True, smtp_host="test.invalid", smtp_port=465, smtp_user="test", smtp_password="fixture-only")
    monkeypatch.setattr(NotificationService, "get_channel_by_id", AsyncMock(return_value=channel))
    monkeypatch.setattr(NotificationRecipientService, "get_enabled_emails", AsyncMock(return_value=["test@example.invalid"]))
    sender = AsyncMock()
    monkeypatch.setattr(EmailNotificationService, "send_email", sender)
    ok, reason = await NotificationService.send_notification(1, "stale", "stale", alert_type="node_down", node_name="audit-node", incident_id=incident["id"], check_limits=False)
    assert not ok and "取消" in reason
    sender.assert_not_awaited()


async def test_all_node_stats_do_not_use_short_memory_for_long_range(monkeypatch):
    from app.routes import nodes
    from app.services.availability import AvailabilityService, HeartbeatRecord
    service = AvailabilityService()
    service.add_record(HeartbeatRecord(node_name="audit-node", timestamp=datetime.now(timezone.utc).replace(tzinfo=None), success=True, latency_ms=5, status_code=200))
    monkeypatch.setattr(nodes, "get_availability_service", lambda: service)
    repo = SimpleNamespace(get_aggregated_uptime=AsyncMock(side_effect=[{}, {"audit-node":{"uptime":75}}]))
    monkeypatch.setattr(nodes, "_get_db_repository", lambda: repo)
    output = await nodes.get_all_availability()
    assert output["nodes"][0]["uptime_7d"] is None
    assert output["nodes"][0]["uptime_30d"] == 75


def test_credential_scanner_distinguishes_literals_and_configuration():
    from scripts.check_secrets import literal_credentials
    assert literal_credentials('smtp_password = os.getenv("SMTP_PASSWORD")') == []
    assert literal_credentials('smtp_password = Column(String(500))') == []
    assert literal_credentials('smtp_password = "fixture-only"') == [1]


async def test_legacy_successful_alert_is_delivery_evidence(sql_store):
    from app.models.notification_channel import AlertLogModel
    incident = await seed_incident()
    with Session(sql_store[0]) as session:
        session.add(AlertLogModel(node_name="audit-node", alert_type="node_down", message="old log", incident_id=incident["id"], notification_sent=True))
        session.commit()
    assert await IncidentService.has_delivered_down_alert(incident["id"]) is True


def test_legacy_time_repair_requires_samples_and_is_idempotent():
    from scripts.repair_incident_times import corrected_resolution
    start = datetime(2026, 10, 12)
    row = dict(started_at=start, resolved_at=start+timedelta(hours=8,minutes=5), duration_seconds=29100, first_success=start+timedelta(minutes=5))
    assert corrected_resolution(row,8) == row["first_success"]
    row.update(resolved_at=row["first_success"],duration_seconds=300)
    assert corrected_resolution(row,8) is None
    row["first_success"] = None
    assert corrected_resolution(row,8) is None
