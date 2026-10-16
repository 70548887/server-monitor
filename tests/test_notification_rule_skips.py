"""Rule suppression is an expected skip; transport errors remain failed attempts."""

from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session

from app.models.base import Base
from app.models.notification_channel import (
    NotificationChannelModel,
    NotificationLogModel,
)
from app.services import notification
from app.services.notification import NotificationLogService, NotificationService


NOW = datetime(2026, 10, 16, 12, 0)
SKIP_REASON = "节点 rule-node 今日发送已达上限（20次）"


@pytest.fixture
def rule_store(monkeypatch):
    """Run actual ORM queries with SQLite, replacing only the async driver boundary."""
    engine = create_engine("sqlite:///:memory:")

    @event.listens_for(engine, "connect")
    def fixed_database_clock(connection, _record):
        connection.create_function("current_timestamp", 0, lambda: NOW.strftime("%Y-%m-%d %H:%M:%S"))

    Base.metadata.create_all(engine)

    class AsyncSessionAdapter:
        def __init__(self, session):
            self.session = session

        async def execute(self, statement):
            return self.session.execute(statement)

        def add(self, obj):
            self.session.add(obj)

        async def delete(self, obj):
            self.session.delete(obj)

        async def flush(self):
            self.session.flush()

        async def refresh(self, obj):
            self.session.refresh(obj)

    @asynccontextmanager
    async def context():
        with Session(engine, expire_on_commit=False) as session:
            try:
                yield AsyncSessionAdapter(session)
                session.commit()
            except Exception:
                session.rollback()
                raise

    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW if tz is None else NOW.replace(tzinfo=tz)

        @classmethod
        def today(cls):
            return NOW

    monkeypatch.setattr(notification, "get_session_context", context)
    monkeypatch.setattr(notification, "datetime", FixedDatetime)
    yield engine
    engine.dispose()


def make_log(success=False, error=None, sent_at=NOW, channel_id=1, **kwargs):
    return NotificationLogModel(
        channel_id=channel_id,
        channel_name=f"channel-{channel_id}",
        node_name="rule-node",
        alert_type="node_down",
        success=success,
        error_message=error,
        sent_at=sent_at,
        **kwargs,
    )


def seed(engine, *rows):
    with Session(engine, expire_on_commit=False) as session:
        session.add_all(rows)
        session.commit()
        return [row.id for row in rows]


def channel_counts(engine, channel_id=1):
    with Session(engine) as session:
        row = session.get(NotificationChannelModel, channel_id)
        return row.send_count, row.error_count, row.last_error


@pytest.mark.parametrize(
    "success,error,status,reason",
    [
        (True, None, "success", None),
        (True, f"发送被限制: {SKIP_REASON}", "success", None),
        (False, f"发送被限制: {SKIP_REASON}", "skipped", SKIP_REASON),
        (False, f"发送被限制： {SKIP_REASON}", "skipped", SKIP_REASON),
        (False, "SMTP authentication failed", "failed", None),
        (False, "SMTP: 发送被限制: remote server rejected", "failed", None),
        (False, None, "failed", None),
    ],
)
def test_serialization_classifies_legacy_rules_without_rewriting_evidence(
    success, error, status, reason
):
    row = make_log(success=success, error=error)
    result = row.to_dict()
    assert row.delivery_status == status
    assert result["status"] == status
    assert result["skip_reason"] == reason
    assert result["success"] is success
    assert result["error_message"] == (None if status == "skipped" else error)
    assert row.error_message == error


async def test_filters_keep_rule_skips_out_of_legacy_failed_results(rule_store):
    ids = seed(
        rule_store,
        make_log(success=True, sent_at=NOW - timedelta(seconds=4)),
        make_log(error="SMTP refused", sent_at=NOW - timedelta(seconds=3)),
        make_log(error=f"发送被限制: {SKIP_REASON}", sent_at=NOW - timedelta(seconds=2)),
        make_log(error="发送被限制：发送间隔太短", sent_at=NOW - timedelta(seconds=1)),
        make_log(error="SMTP unreachable", channel_id=2),
    )
    failed, total = await NotificationLogService.get_logs(channel_id=1, success=False)
    assert total == 1
    assert [row["id"] for row in failed] == [ids[1]]
    assert failed[0]["status"] == "failed"

    skipped, total = await NotificationLogService.get_logs(
        channel_id=1, status="skipped", limit=1, offset=1
    )
    assert total == 2
    assert [row["id"] for row in skipped] == [ids[2]]
    assert skipped[0]["skip_reason"] == SKIP_REASON
    for status, expected_id in (("success", ids[0]), ("failed", ids[1])):
        rows, total = await NotificationLogService.get_logs(channel_id=1, status=status)
        assert total == 1
        assert [row["id"] for row in rows] == [expected_id]
    all_rows, total = await NotificationLogService.get_logs(channel_id=1)
    assert total == 4 and len(all_rows) == 4
    with Session(rule_store) as session:
        assert session.get(NotificationLogModel, ids[2]).error_message == f"发送被限制: {SKIP_REASON}"


async def test_today_stats_aggregate_beyond_one_page_and_exclude_yesterday(rule_store):
    seed(
        rule_store,
        *(make_log(success=True) for _ in range(1007)),
        *(make_log(error="SMTP timeout") for _ in range(3)),
        *(make_log(error=f"发送被限制: {SKIP_REASON}") for _ in range(5)),
        *(make_log(error="发送被限制：不在允许发送时段") for _ in range(2)),
        make_log(success=True, sent_at=NOW - timedelta(days=1)),
        make_log(error="SMTP old", sent_at=NOW - timedelta(days=1)),
    )
    assert await NotificationLogService.get_today_stats() == {
        "total": 1010,
        "success": 1007,
        "failed": 3,
        "skipped": 7,
    }


async def test_recalculation_excludes_rules_from_channel_attempt_and_error_counts(rule_store):
    seed(
        rule_store,
        NotificationChannelModel(id=1, name="mixed", channel_type="email", send_count=99, error_count=88),
        NotificationChannelModel(id=2, name="only-skips", channel_type="email", send_count=4, error_count=4),
        make_log(success=True),
        make_log(error="SMTP invalid credentials"),
        make_log(error=f"发送被限制: {SKIP_REASON}"),
        make_log(error="发送被限制：不在发送时间", channel_id=2),
    )
    results = await NotificationLogService.recalculate_channel_stats()
    assert results[1]["new_total"] == 2
    assert results[1]["new_failed"] == 1
    assert results[2]["new_total"] == results[2]["new_failed"] == 0
    assert channel_counts(rule_store)[:2] == (2, 1)
    assert channel_counts(rule_store, 2)[:2] == (0, 0)


async def test_deleting_one_skip_leaves_actual_channel_counters_unchanged(rule_store):
    ids = seed(
        rule_store,
        NotificationChannelModel(id=1, name="mixed", channel_type="email", send_count=2, error_count=1, last_error="SMTP timeout"),
        make_log(error=f"发送被限制: {SKIP_REASON}"),
    )
    assert await NotificationLogService.delete_log(ids[1]) is True
    assert channel_counts(rule_store) == (2, 1, "SMTP timeout")
    assert await NotificationLogService.delete_log(ids[1]) is False


async def test_batch_delete_changes_counters_only_for_actual_attempts(rule_store):
    ids = seed(
        rule_store,
        NotificationChannelModel(id=1, name="mixed", channel_type="email", send_count=2, error_count=1),
        make_log(success=True),
        make_log(error="SMTP timeout"),
        make_log(error=f"发送被限制: {SKIP_REASON}"),
        make_log(error="发送被限制：间隔太短"),
    )
    assert await NotificationLogService.delete_logs_batch([ids[1], ids[3], ids[4], 9999]) == 3
    assert channel_counts(rule_store)[:2] == (1, 1)
    assert await NotificationLogService.delete_logs_batch([ids[2]]) == 1
    assert channel_counts(rule_store)[:2] == (0, 0)


async def test_skip_does_not_restart_minimum_send_interval(rule_store):
    actual_attempt = NOW - timedelta(seconds=40)
    seed(
        rule_store,
        make_log(success=True, sent_at=NOW - timedelta(seconds=60)),
        make_log(error="SMTP timeout", sent_at=actual_attempt),
        make_log(error="发送被限制: 发送间隔太短", sent_at=NOW - timedelta(seconds=5)),
        make_log(error="发送被限制：发送间隔太短", sent_at=NOW),
    )
    assert await NotificationLogService.get_last_send_time(1, "rule-node") == actual_attempt
    assert await NotificationLogService.get_last_send_time(1) == actual_attempt
    assert await NotificationService.check_send_limits(
        {"id": 1, "min_interval_seconds": 30, "daily_limit_per_node": 0}, "rule-node"
    ) == (True, "")


@pytest.mark.parametrize("rule", ["daily-cap", "minimum-interval", "send-window"])
async def test_blocked_rules_never_attempt_transport_or_mark_channel_failed(rule_store, monkeypatch, rule):
    values = dict(daily_limit_per_node=0, min_interval_seconds=0)
    if rule == "daily-cap":
        values["daily_limit_per_node"] = 1
    elif rule == "minimum-interval":
        values["min_interval_seconds"] = 30
    else:
        from datetime import time

        values.update(send_time_start=time(13), send_time_end=time(14))
    seed(
        rule_store,
        NotificationChannelModel(id=1, name="rule-channel", channel_type="email", enabled=True, send_count=1, error_count=0, **values),
        make_log(success=True, sent_at=NOW - timedelta(seconds=10)),
    )
    email = AsyncMock()
    webhook = AsyncMock()
    monkeypatch.setattr(notification.EmailNotificationService, "send_email", email)
    monkeypatch.setattr(notification.WebhookNotificationService, "send_webhook", webhook)
    result, reason = await NotificationService.send_notification(
        1, "Node unavailable", "Still down", node_name="rule-node", alert_type="node_down"
    )
    assert result is False and reason
    email.assert_not_awaited()
    webhook.assert_not_awaited()
    assert channel_counts(rule_store) == (1, 0, None)
    logs, total = await NotificationLogService.get_logs(status="skipped")
    assert total == 1
    assert logs[0]["status"] == "skipped"
    assert logs[0]["skip_reason"] == reason
    assert logs[0]["error_message"] is None
    assert logs[0]["send_duration_ms"] is None
    assert await NotificationLogService.get_today_stats() == {
        "total": 1, "success": 1, "failed": 0, "skipped": 1
    }


@pytest.mark.parametrize("channel_type", ["email", "webhook"])
async def test_real_transport_failure_keeps_error_status_and_counters(rule_store, monkeypatch, channel_type):
    seed(
        rule_store,
        NotificationChannelModel(
            id=1, name="transport", channel_type=channel_type, enabled=True,
            smtp_host="smtp.invalid", smtp_port=465, smtp_user="sender@example.invalid",
            smtp_password="fixture-only", webhook_url="http://webhook.invalid",
            daily_limit_per_node=0, min_interval_seconds=0,
        ),
    )
    monkeypatch.setattr(notification.NotificationRecipientService, "get_enabled_emails", AsyncMock(return_value=["target@example.invalid"]))
    sender = AsyncMock(return_value=(False, "Remote transport refused"))
    if channel_type == "email":
        monkeypatch.setattr(notification.EmailNotificationService, "send_email", sender)
    else:
        monkeypatch.setattr(notification.WebhookNotificationService, "send_webhook", sender)
    assert await NotificationService.send_notification(
        1, "Node down", "Still unavailable", node_name="rule-node", alert_type="node_down"
    ) == (False, "Remote transport refused")
    sender.assert_awaited_once()
    assert channel_counts(rule_store) == (1, 1, "Remote transport refused")
    logs, total = await NotificationLogService.get_logs(success=False)
    assert total == 1
    assert logs[0]["status"] == "failed"
    assert logs[0]["error_message"] == "Remote transport refused"
    assert logs[0]["skip_reason"] is None


async def test_api_exposes_rule_status_and_rejects_invalid_filter(rule_store):
    from app.routes.notifications import router

    seed(
        rule_store,
        make_log(success=True),
        make_log(error="SMTP rejected"),
        make_log(error=f"发送被限制: {SKIP_REASON}"),
    )
    app = FastAPI()
    app.include_router(router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/notifications/logs", params={"status": "skipped"})
        assert response.status_code == 200
        body = response.json()
        assert body["total"] == 1
        assert body["logs"][0]["status"] == "skipped"
        assert body["logs"][0]["skip_reason"] == SKIP_REASON
        assert body["logs"][0]["error_message"] is None
        legacy = await client.get("/api/notifications/logs", params={"success": "false"})
        assert legacy.status_code == 200 and legacy.json()["total"] == 1
        stats = await client.get("/api/notifications/logs/stats")
        assert stats.status_code == 200
        assert stats.json()["today"] == {"total": 2, "success": 1, "failed": 1, "skipped": 1}
        invalid = await client.get("/api/notifications/logs", params={"status": "unknown"})
        assert invalid.status_code == 422
