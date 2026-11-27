from __future__ import annotations

import os
from pathlib import Path

from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.engine import Engine

from aitrace.api.app import create_app
from aitrace.core.settings import (
    DatabaseSettings,
    RuntimeSettings,
    Settings,
    StorageSettings,
)

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")

HEADERS = {"Authorization": "Bearer test-service-token"}

_TRUNCATE = """
TRUNCATE TABLE audit_log, attachment, profile, memory_hot_file, case_contribution,
case_library, user_queue, task_queue, batch, report, "round", "session", callback_seq
RESTART IDENTITY CASCADE
"""


def _payload() -> dict[str, object]:
    return {
        "product_id": "ssd_9560",
        "batch_id": "B-RED-1",
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
                    "sn": "SN-RED-1",
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
                "case_id": "CASE-RED-1",
                "script_path": "tests/redteam/test_upload.py::test_scan",
                "failed_at_utc": 1795105200,
                "env_binding": {"device_id": "D1"},
                "logs": {
                    "test_log_path": "/logs/test.log",
                    "uart_log_path": "/logs/uart.log",
                },
            }
        ],
    }


def _isolate(db_engine: Engine) -> None:
    with db_engine.begin() as conn:
        conn.execute(text(_TRUNCATE))


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        service_token=SecretStr("test-service-token"),
        database=DatabaseSettings(dsn=TEST_DATABASE_URL or ""),
        runtime=RuntimeSettings(enabled=False),
        storage=StorageSettings(data_dir=tmp_path / "data"),
    )


def _deliver(client: TestClient) -> str:
    response = client.post("/api/v1/aitrace/tasks", json=_payload(), headers=HEADERS)
    assert response.status_code == 202, response.text
    return response.json()["sessions"][0]["session_id"]


def _upload(client: TestClient, session_id: str, filename: str, content: bytes, media_type: str):
    return client.post(
        f"/api/v1/aitrace/sessions/{session_id}/attachments",
        files={"file": (filename, content, media_type)},
        data={"uploaded_by": "zhangsan"},
        headers=HEADERS,
    )


def _skip_without_db() -> None:
    if TEST_DATABASE_URL is None:
        import pytest

        pytest.skip("TEST_DATABASE_URL is not set")


class TestRedteamAttachments:
    def test_elf_magic_rejected(self, db_engine: Engine, tmp_path: Path) -> None:
        _skip_without_db()
        _isolate(db_engine)
        with TestClient(create_app(_settings(tmp_path))) as client:
            session_id = _deliver(client)
            response = _upload(
                client,
                session_id,
                "evil.txt",
                b"\x7fELF\x02\x01\x01" + b"\x00" * 32,
                "text/plain",
            )
        assert response.status_code == 422
        body = response.json()
        assert body["code"] == "ATTACHMENT_MALICIOUS"
        assert "ELF" in body["message"]
        uploads_dir = tmp_path / "data" / "rawlogs" / "B-RED-1" / "uploads"
        assert not list(uploads_dir.glob("A-*"))

    def test_shebang_script_rejected(self, db_engine: Engine, tmp_path: Path) -> None:
        _skip_without_db()
        _isolate(db_engine)
        with TestClient(create_app(_settings(tmp_path))) as client:
            session_id = _deliver(client)
            response = _upload(
                client,
                session_id,
                "cleanup.sh",
                b"#!/bin/sh\nrm -rf /data\nreboot\n",
                "text/plain",
            )
        assert response.status_code == 422
        assert response.json()["code"] == "ATTACHMENT_MALICIOUS"

    def test_pe_magic_rejected(self, db_engine: Engine, tmp_path: Path) -> None:
        _skip_without_db()
        _isolate(db_engine)
        with TestClient(create_app(_settings(tmp_path))) as client:
            session_id = _deliver(client)
            response = _upload(
                client,
                session_id,
                "p.exe",
                b"MZ\x90\x00" + b"\x00" * 32,
                "text/plain",
            )
        assert response.status_code == 422
        assert response.json()["code"] == "ATTACHMENT_MALICIOUS"

    def test_unlisted_media_type_rejected(self, db_engine: Engine, tmp_path: Path) -> None:
        _skip_without_db()
        _isolate(db_engine)
        with TestClient(create_app(_settings(tmp_path))) as client:
            session_id = _deliver(client)
            response = _upload(client, session_id, "x.sh", b"rm -rf /", "application/x-sh")
        assert response.status_code == 415
        body = response.json()
        assert body["code"] == "ATTACHMENT_TYPE_FORBIDDEN"
        assert "application/x-sh" in body["message"]
        assert "text/plain" in body["message"]

    def test_hostile_text_content_still_allowed(self, db_engine: Engine, tmp_path: Path) -> None:
        """攻击字符串出现在日志文本内是数据而非指令——不得误伤（防误报断言）。"""
        _skip_without_db()
        _isolate(db_engine)
        with TestClient(create_app(_settings(tmp_path))) as client:
            session_id = _deliver(client)
            content = b"rm -rf / && reboot\n#!/bin/sh\ndd if=/dev/zero of=/dev/nvme0n1\n"
            response = _upload(client, session_id, "around.log", content, "text/plain")
        assert response.status_code == 200
        assert response.json()["sha256"] != ""

    def test_benign_pdf_allowed(self, db_engine: Engine, tmp_path: Path) -> None:
        _skip_without_db()
        _isolate(db_engine)
        with TestClient(create_app(_settings(tmp_path))) as client:
            session_id = _deliver(client)
            response = _upload(
                client, session_id, "report.pdf", b"%PDF-1.7\n%%EOF\n", "application/pdf"
            )
        assert response.status_code == 200
