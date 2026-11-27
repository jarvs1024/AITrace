from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.engine import Engine

from aitrace.api.app import create_app
from aitrace.core.settings import (
    DatabaseSettings,
    QueueSettings,
    RuntimeSettings,
    Settings,
    StorageSettings,
)

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")

HEADERS = {"Authorization": "Bearer test-service-token"}


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
        "passed_cases": [{"case_id": "CASE-0987", "script_path": "tests/ok.py"}],
    }
    payload.update(overrides)
    return payload


def _isolate_tables(db_engine: Engine) -> None:
    with db_engine.begin() as conn:
        conn.execute(
            text(
                'TRUNCATE TABLE task_queue, user_queue, batch, report, "round", '
                '"session", profile RESTART IDENTITY CASCADE'
            )
        )


def _settings(
    tmp_path: Path,
    *,
    queue: dict[str, object] | None = None,
    storage: dict[str, object] | None = None,
) -> Settings:
    return Settings(
        service_token=SecretStr("test-service-token"),
        database=DatabaseSettings(dsn=TEST_DATABASE_URL or ""),
        runtime=RuntimeSettings(enabled=False),
        queue=QueueSettings(**(queue or {})),
        storage=StorageSettings(data_dir=tmp_path / "data", **(storage or {})),
    )


def _seed_pending(db_engine: Engine, count: int, queue_key: str = "zhangsan") -> None:
    stmt = text(
        "INSERT INTO task_queue "
        "(task_id, batch_id, session_id, round_id, round_no, product_id, case_id, "
        "task_type, queue_key, queue_state, priority, attempts, max_attempts, "
        "lease_generation, payload, callback_status, created_at, updated_at) VALUES "
        "(:tid, :bid, NULL, :rid, 1, 'ssd_9560', :cid, 'case_clean', :qk, "
        "'PENDING', 1, 0, 3, 0, '{}'::jsonb, 'PENDING', now(), now())"
    )
    with db_engine.begin() as conn:
        for i in range(count):
            conn.execute(
                stmt,
                {
                    "tid": f"T-SEED-{i}",
                    "bid": f"B-SEED-{i}",
                    "rid": f"R-SEED-{i}",
                    "cid": f"CASE-SEED-{i}",
                    "qk": queue_key,
                },
            )


def test_global_depth_over_limit_yields_queue_full(db_engine: Engine, tmp_path: Path) -> None:
    if TEST_DATABASE_URL is None:
        import pytest

        pytest.skip("TEST_DATABASE_URL is not set")
    _isolate_tables(db_engine)
    settings = _settings(tmp_path, queue={"global_depth": 5})
    _seed_pending(db_engine, 5)
    with TestClient(create_app(settings)) as client:
        response = client.post("/api/v1/aitrace/tasks", json=_payload(), headers=HEADERS)
    assert response.status_code == 429
    body = response.json()
    assert body["code"] == "QUEUE_FULL"
    assert body["retryable"] is True
    assert body["retry_after"] == 60
    assert "当前 5" in body["message"] and "全局" in body["message"]
    with db_engine.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM task_queue")).scalar_one() == 5


def test_trigger_token_depth_over_limit_yields_queue_full(
    db_engine: Engine, tmp_path: Path
) -> None:
    if TEST_DATABASE_URL is None:
        import pytest

        pytest.skip("TEST_DATABASE_URL is not set")
    _isolate_tables(db_engine)
    settings = _settings(tmp_path, queue={"per_token_depth": 1, "global_depth": 500})
    _seed_pending(db_engine, 1, queue_key="zhangsan")
    with TestClient(create_app(settings)) as client:
        response = client.post("/api/v1/aitrace/tasks", json=_payload(), headers=HEADERS)
    assert response.status_code == 429
    body = response.json()
    assert body["code"] == "QUEUE_FULL"
    assert body["retryable"] is True
    assert "zhangsan" in body["message"]
    with db_engine.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM task_queue")).scalar_one() == 1


def test_full_disk_after_eviction_yields_disk_full(db_engine: Engine, tmp_path: Path) -> None:
    if TEST_DATABASE_URL is None:
        import pytest

        pytest.skip("TEST_DATABASE_URL is not set")
    _isolate_tables(db_engine)
    settings = _settings(tmp_path, storage={"l2_quota_bytes": 1000})
    rawlogs = settings.storage.data_dir / "rawlogs"
    rawlogs.mkdir(parents=True)
    (rawlogs / "filler.log").write_bytes(b"x" * 900)
    with TestClient(create_app(settings)) as client:
        response = client.post("/api/v1/aitrace/tasks", json=_payload(), headers=HEADERS)
    assert response.status_code == 507
    body = response.json()
    assert body["code"] == "DISK_FULL"
    assert body["retryable"] is True
    assert "90" in body["message"]
    with db_engine.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM batch")).scalar_one() == 0


def _seed_stale_evidence(db_engine: Engine, settings: Settings) -> None:
    now = datetime.now(UTC)
    finished = now - timedelta(days=40)
    with db_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO batch (batch_id, product_id, trigger_user_id, batch_task_id, "
                "failed_cases, passed_cases, batch_state, updated_at) VALUES "
                "('B-OLD', 'ssd_9560', 'zhangsan', 'T-B-OLD', '[]'::jsonb, '[]'::jsonb, "
                "'COMPLETED', :now)"
            ),
            {"now": finished},
        )
        conn.execute(
            text(
                'INSERT INTO "session" (session_id, product_id, batch_id, case_id, domain_id, '
                "profile_version, trigger_user_id, hardware_type, env_topology, git_context, "
                "latest_round_no, status, created_at) VALUES "
                "('S-OLD', 'ssd_9560', 'B-OLD', 'CASE-OLD', 'enterprise', 'v1', 'zhangsan', "
                "'SSD', '{}'::jsonb, '{}'::jsonb, 1, 'RUNNING', :now)"
            ),
            {"now": finished},
        )
        conn.execute(
            text(
                'INSERT INTO "round" (round_id, session_id, round_no, trigger_user_id, '
                "queue_key, stage, round_status, started_at, finished_at) VALUES "
                "('R-OLD', 'S-OLD', 1, 'zhangsan', 'zhangsan', 'COMPLETED', 'VALID', "
                ":now, :finished)"
            ),
            {"now": finished, "finished": finished},
        )
    stale_dir = settings.storage.data_dir / "evidence" / "S-OLD" / "round_1"
    stale_dir.mkdir(parents=True)
    (stale_dir / "summary.json").write_text("{}", encoding="utf-8")


def test_fraction_over_warn_triggers_eviction_then_accepts(
    db_engine: Engine, tmp_path: Path
) -> None:
    if TEST_DATABASE_URL is None:
        import pytest

        pytest.skip("TEST_DATABASE_URL is not set")
    _isolate_tables(db_engine)
    settings = _settings(
        tmp_path,
        storage={"l2_quota_bytes": 1000, "l1_ttl_days": 10, "l2_ttl_days": 10},
    )
    _seed_stale_evidence(db_engine, settings)
    rawlogs = settings.storage.data_dir / "rawlogs"
    rawlogs.mkdir(parents=True)
    (rawlogs / "filler.log").write_bytes(b"x" * 700)
    with TestClient(create_app(settings)) as client:
        response = client.post("/api/v1/aitrace/tasks", json=_payload(), headers=HEADERS)
    assert response.status_code == 202
    assert not (settings.storage.data_dir / "evidence" / "S-OLD").exists()
    with db_engine.connect() as conn:
        # 淘汰仅清理数据目录，DB 轮次行保留作审计；新投递追加 1 轮 → 共 2 行
        assert conn.execute(text('SELECT COUNT(*) FROM "round"')).scalar_one() == 2
