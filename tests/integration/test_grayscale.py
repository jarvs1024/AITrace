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
    GrayscaleSettings,
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


def _payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "product_id": "ssd_9560",
        "batch_id": "BATCH-20261120-0089",
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
                    "sn": "SN-20240315-XXXX",
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
                "case_id": "CASE-1021",
                "script_path": "tests/stress/test_io_reset.py::test_hot_reset",
                "failed_at_utc": 1795105200,
                "env_binding": {"device_id": "D1"},
                "logs": {"test_log_path": "/logs/test.log", "uart_log_path": "/logs/uart.log"},
            }
        ],
    }
    payload.update(overrides)
    return payload


def _isolate(db_engine: Engine) -> None:
    with db_engine.begin() as conn:
        conn.execute(text(_TRUNCATE))


def _settings(tmp_path: Path, *, grayscale: dict[str, object]) -> Settings:
    return Settings(
        service_token=SecretStr("test-service-token"),
        database=DatabaseSettings(dsn=TEST_DATABASE_URL or ""),
        runtime=RuntimeSettings(enabled=False),
        storage=StorageSettings(data_dir=tmp_path / "data"),
        grayscale=GrayscaleSettings(**grayscale),
    )


def _skip_without_db() -> None:
    if TEST_DATABASE_URL is None:
        import pytest

        pytest.skip("TEST_DATABASE_URL is not set")


def test_grayscale_disabled_accepts(db_engine: Engine, tmp_path: Path) -> None:
    _skip_without_db()
    _isolate(db_engine)
    settings = _settings(tmp_path, grayscale={})
    with TestClient(create_app(settings)) as client:
        response = client.post("/api/v1/aitrace/tasks", json=_payload(), headers=HEADERS)
    assert response.status_code == 202


def test_grayscale_enabled_allows_listed_product(db_engine: Engine, tmp_path: Path) -> None:
    _skip_without_db()
    _isolate(db_engine)
    settings = _settings(tmp_path, grayscale={"enabled": True, "product_ids": ["ssd_9560"]})
    with TestClient(create_app(settings)) as client:
        response = client.post("/api/v1/aitrace/tasks", json=_payload(), headers=HEADERS)
    assert response.status_code == 202


def test_grayscale_enabled_blocks_unlisted_product(db_engine: Engine, tmp_path: Path) -> None:
    _skip_without_db()
    _isolate(db_engine)
    settings = _settings(tmp_path, grayscale={"enabled": True, "product_ids": ["cxl_9600"]})
    with TestClient(create_app(settings)) as client:
        response = client.post("/api/v1/aitrace/tasks", json=_payload(), headers=HEADERS)
    assert response.status_code == 403
    body = response.json()
    assert body["code"] == "GRAYSCALE_BLOCKED"
    assert "ssd_9560" in body["message"]
    assert "清单" in body["message"]
    assert body["retryable"] is False


def test_grayscale_enabled_with_empty_list_rejects_all(db_engine: Engine, tmp_path: Path) -> None:
    _skip_without_db()
    _isolate(db_engine)
    settings = _settings(tmp_path, grayscale={"enabled": True, "product_ids": []})
    with TestClient(create_app(settings)) as client:
        response = client.post("/api/v1/aitrace/tasks", json=_payload(), headers=HEADERS)
    assert response.status_code == 403
    assert response.json()["code"] == "GRAYSCALE_BLOCKED"
