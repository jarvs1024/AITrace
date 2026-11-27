from __future__ import annotations

import logging
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker
from tests.conftest import requires_db

from aitrace.ai.qoder import FakeQoder
from aitrace.ai.worker import AIWorker
from aitrace.api.app import create_app
from aitrace.core.audit import AuditService
from aitrace.core.ids import UuidIdGenerator
from aitrace.core.settings import (
    AISettings,
    AitestSettings,
    DatabaseSettings,
    RuntimeSettings,
    Settings,
    StorageSettings,
)
from aitrace.persistence.models import TaskQueue

pytestmark = requires_db

HOSTILE_TOKEN = 'qk_9f3a\n"<script>alert(1)</script>" \\ {unused}'
SENTINEL_MARKER = "SENTINEL_REDTEAM_9f3a_DO_NOT_LOG"

HEADERS = {"Authorization": "Bearer test-service-token"}


@pytest.fixture(autouse=True)
def _isolate(db_engine: Engine) -> None:
    with db_engine.begin() as conn:
        conn.execute(
            text(
                "TRUNCATE TABLE audit_log, task_queue, user_queue, batch, report, "
                '"round", "session", profile RESTART IDENTITY CASCADE'
            )
        )


@pytest.fixture
def audit(db_engine: Engine) -> AuditService:
    return AuditService(sessionmaker(bind=db_engine, expire_on_commit=False))


def _make_worker(audit: AuditService, transport: httpx.BaseTransport, tmp_path: Path) -> AIWorker:
    return AIWorker(
        settings=AISettings(),
        aitest=AitestSettings(base_url="https://aitest.internal", read_token=SecretStr("rt")),
        adapter=FakeQoder([]),
        token_transport=transport,
        base_dir=tmp_path,
        query_timeout=60.0,
        ids=UuidIdGenerator(),
        audit=audit,
    )


def _task() -> TaskQueue:
    return TaskQueue(
        product_id="ssd_9560",
        batch_id="B-T-1",
        case_id="C-T-1",
        queue_key="zhangsan",
    )


def _grant_handler(request: httpx.Request) -> httpx.Response:
    assert request.headers["X-Aitest-Read-Token"] == "rt"
    return httpx.Response(200, json={"token": HOSTILE_TOKEN, "expires_at": 4102444800})


class TestJitFetchAudit:
    def test_success_audits_keys_without_token_body(
        self, audit: AuditService, db_engine: Engine, tmp_path: Path
    ) -> None:
        worker = _make_worker(audit, httpx.MockTransport(_grant_handler), tmp_path)
        grant = worker._fetch_token(_task())
        assert grant.token == HOSTILE_TOKEN
        with sessionmaker(bind=db_engine)() as session:
            rows = (
                session.execute(text("SELECT action, target, detail FROM audit_log"))
                .mappings()
                .all()
            )
        assert len(rows) == 1
        assert rows[0]["action"] == "TOKEN_FETCH"
        assert rows[0]["target"] == "zhangsan"
        detail = rows[0]["detail"]
        assert detail["result"] == "ok"
        assert detail["product_id"] == "ssd_9560"
        assert detail["batch_id"] == "B-T-1"
        assert detail["case_id"] == "C-T-1"
        assert detail["trigger_user_id"] == "zhangsan"
        assert "token" not in detail
        with sessionmaker(bind=db_engine)() as session:
            leaked = session.execute(
                text("SELECT count(*) FROM audit_log WHERE detail::text LIKE '%<script>%'")
            ).scalar_one()
        assert leaked == 0

    def test_failure_audits_code_and_retryable_without_token(
        self, audit: AuditService, db_engine: Engine, tmp_path: Path
    ) -> None:
        def forbidden(request: httpx.Request) -> httpx.Response:
            return httpx.Response(403, json={"code": "FORBIDDEN", "message": "用户未授权"})

        worker = _make_worker(audit, httpx.MockTransport(forbidden), tmp_path)
        with pytest.raises(Exception) as caught:
            worker._fetch_token(_task())
        assert caught.value.code == "FORBIDDEN"
        with sessionmaker(bind=db_engine)() as session:
            row = (
                session.execute(text("SELECT detail FROM audit_log ORDER BY id"))
                .mappings()
                .all()[-1]
            )
        detail = row["detail"]
        assert detail["result"] == "fail"
        assert detail["code"] == "FORBIDDEN"
        assert detail["retryable"] is False
        assert "token" not in detail


class TestServiceLogHygiene:
    def test_delivery_never_echoes_payload_in_service_logs(
        self, db_engine: Engine, tmp_path: Path, caplog
    ) -> None:
        settings = Settings(
            service_token=SecretStr("test-service-token"),
            database=DatabaseSettings(dsn=db_engine.url.render_as_string(hide_password=False)),
            runtime=RuntimeSettings(enabled=False),
            storage=StorageSettings(data_dir=tmp_path / "data"),
        )
        payload = {
            "product_id": "ssd_9560",
            "batch_id": "B-HYG-1",
            "trigger_user_id": "zhangsan",
            "hardware_type": "SSD",
            "env_topology": {
                "host_ip": "10.12.8.45",
                "host_boot_time_utc": 1795104000,
                "devices": [
                    {
                        "device_id": "D1",
                        "pcie_bdf": "0000:3b:00.0",
                        "device_node": "/dev/nvme0n1",
                        "model": "AliFlash V6 3.84T",
                        "sn": "SN-HYG-1",
                        "fw_version": "8.1.2.3",
                        "slot": "SLOT-3",
                    }
                ],
            },
            "git_context": {
                "repo_url": "https://gitlab.internal/ssd-qa/nvme-auto.git",
                "commit_id": "a9c4e12f",
            },
            "failed_cases": [
                {
                    "case_id": "CASE-HYG-1",
                    "script_path": f"tests/redteam/{SENTINEL_MARKER}.py::test_x",
                    "failed_at_utc": 1795105200,
                    "env_binding": {"device_id": "D1"},
                    "logs": {"test_log_path": "/logs/test.log"},
                }
            ],
        }
        with (
            TestClient(create_app(settings)) as client,
            caplog.at_level(logging.DEBUG, logger="aitrace"),
        ):
            response = client.post("/api/v1/aitrace/tasks", json=payload, headers=HEADERS)
        assert response.status_code == 202
        assert SENTINEL_MARKER not in caplog.text
        with db_engine.connect() as conn:
            leaked = conn.execute(
                text("SELECT count(*) FROM audit_log WHERE detail::text LIKE :marker"),
                {"marker": f"%{SENTINEL_MARKER}%"},
            ).scalar_one()
        assert leaked == 0
