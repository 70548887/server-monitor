"""Incident detail API regressions with real SQL and no application lifespan."""

from contextlib import asynccontextmanager
from datetime import datetime, timedelta
import logging

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import MetaData, Table, create_engine, select, text
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.models.notification_channel import NotificationLogModel
from app.routes.notifications import router


@pytest.fixture
def incident_api(monkeypatch):
    engine = create_engine(
        "sqlite://", poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )

    @asynccontextmanager
    async def session_context():
        with Session(engine) as session:
            class AsyncSessionAdapter:
                async def execute(self, statement):
                    return session.execute(statement)

            try:
                yield AsyncSessionAdapter()
                session.commit()
            except Exception:
                session.rollback()
                raise

    monkeypatch.setattr("app.models.base.get_session_context", session_context)
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as client:
        yield engine, client
    engine.dispose()


def test_incident_notifications_filter_sort_limit_and_serialize(incident_api):
    engine, client = incident_api
    NotificationLogModel.__table__.create(engine)
    started_at = datetime(2026, 10, 14, 10, 55)
    with Session(engine) as session:
        for row_id, incident_id, minutes in [(1, 7, 20), (2, 8, 0), (3, 7, 10), (4, 7, 30)]:
            session.add(NotificationLogModel(
                id=row_id, incident_id=incident_id, notify_seq=minutes // 10,
                channel_id=1, channel_name="test", alert_type="node_down",
                message="a" * 201, success=True,
                sent_at=started_at + timedelta(minutes=minutes),
            ))
        session.commit()

    response = client.get("/api/notifications/incidents/7/notifications?limit=2")
    assert response.status_code == 200
    data = response.json()
    assert data["incident_id"] == 7
    assert data["count"] == 2
    assert [row["id"] for row in data["notifications"]] == [3, 1]
    assert all(row["incident_id"] == 7 for row in data["notifications"])
    assert data["notifications"][0]["sent_at"] == "2026-10-14T11:05:00"
    assert data["notifications"][0]["message"] == "a" * 200 + "..."


def test_incident_notifications_empty_result(incident_api):
    engine, client = incident_api
    NotificationLogModel.__table__.create(engine)
    response = client.get("/api/notifications/incidents/42/notifications")
    assert response.status_code == 200
    assert response.json() == {"incident_id": 42, "notifications": [], "count": 0}


@pytest.mark.parametrize("limit", [0, 501])
def test_incident_notifications_limit_validation(incident_api, limit):
    _, client = incident_api
    response = client.get(f"/api/notifications/incidents/1/notifications?limit={limit}")
    assert response.status_code == 422


def test_old_schema_returns_json_error_and_recovers_after_upgrade(incident_api, caplog):
    engine, client = incident_api
    legacy_metadata = MetaData()
    legacy_table = Table(
        "notification_logs", legacy_metadata,
        *(column._copy() for column in NotificationLogModel.__table__.columns
          if column.name not in {"incident_id", "notify_seq"}),
    )
    legacy_metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(legacy_table.insert(), {
            "id": 12, "channel_id": 1, "channel_name": "legacy-channel",
            "alert_type": "node_down", "message": "retained legacy reminder",
            "sent_at": datetime(2026, 10, 14, 10, 55),
        })

    with caplog.at_level(logging.ERROR, logger="app.routes.notifications"):
        response = client.get("/api/notifications/incidents/7/notifications")
    assert response.status_code == 503
    assert response.headers["content-type"].startswith("application/json")
    assert response.json() == {"detail": {
        "code": "NOTIFICATION_LOGS_UNAVAILABLE",
        "message": "提醒记录暂时无法读取，请检查数据库连接及是否已完成数据库升级。",
    }}
    assert "SELECT" not in response.text
    assert "no such column" not in response.text
    assert "notification_logs.incident_id" not in response.text
    assert "retained legacy reminder" not in response.text
    assert "python scripts/bootstrap_database.py" in caplog.text
    assert any(record.exc_info for record in caplog.records)

    with engine.begin() as connection:
        connection.execute(text("ALTER TABLE notification_logs ADD COLUMN incident_id INT NULL"))
        connection.execute(text("ALTER TABLE notification_logs ADD COLUMN notify_seq INT NULL"))

    response = client.get("/api/notifications/incidents/7/notifications")
    assert response.status_code == 200
    assert response.json() == {"incident_id": 7, "notifications": [], "count": 0}
    with Session(engine) as session:
        original = session.execute(select(NotificationLogModel)).scalar_one()
        assert original.id == 12
        assert original.message == "retained legacy reminder"
        assert original.incident_id is None
        assert original.notify_seq is None
