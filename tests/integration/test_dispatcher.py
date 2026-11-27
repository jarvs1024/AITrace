from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import httpx
from pydantic import SecretStr
from sqlalchemy import select

from aitrace.api.schemas import DeliveryPayload
from aitrace.callback.worker import CallbackWorker
from aitrace.cleaning.registry import CleaningRegistry
from aitrace.cleaning.rules import RuleSet
from aitrace.core.enums import QueueState, Stage, TaskType
from aitrace.core.ids import SystemClock, UuidIdGenerator
from aitrace.core.settings import AISettings, AitestSettings, Settings
from aitrace.ingest.service import IngestService
from aitrace.persistence.models import Report, Round, TaskQueue
from aitrace.profiles.loader import load_profiles
from aitrace.profiles.resolver import ProfileResolver
from aitrace.runtime.dispatcher import build_ai_worker, run_tick


def _deliver(db_session, data_dir: Path) -> str:
    payload = DeliveryPayload.model_validate(
        {
            "product_id": "ssd_9560",
            "batch_id": "BATCH-20261120-0089",
            "trigger_user_id": "wangwu",
            "hardware_type": "SSD",
            "env_topology": {
                "host_ip": "10.20.68.114",
                "host_boot_time_utc": 1768874400,
                "devices": [
                    {
                        "device_id": "nvme0n1",
                        "pcie_bdf": "01:00.0",
                        "device_node": "/dev/nvme0n1",
                        "model": "M9560",
                        "sn": "SN-9560-001",
                        "fw_version": "FW-2.4.1",
                        "slot": "3",
                    }
                ],
            },
            "git_context": {"repo_url": "git@x:y.git", "commit_id": "abc123"},
            "failed_cases": [
                {
                    "case_id": "CASE-wangwu",
                    "script_path": "/cases/power_down_resume.py",
                    "failed_at_utc": 1768877624000,
                    "env_binding": {"device_id": "nvme0n1"},
                    "logs": {
                        "test_log_path": str(data_dir / "test.txt"),
                        "uart_log_path": str(data_dir / "uart.txt"),
                        "kernel_log_path": str(data_dir / "dmesg.txt"),
                        "scheduler_log_path": str(data_dir / "scheduler.txt"),
                    },
                }
            ],
            "passed_cases": [],
        }
    )
    registry = load_profiles(Path("deploy/config/domains"))
    resolver = ProfileResolver(registry)
    outcome = IngestService(db_session, resolver, SystemClock(), UuidIdGenerator()).deliver(payload)
    db_session.flush()
    return outcome.response.sessions[0].session_id


def _mock_transport() -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json={"token": "tok-abc", "expires_at": 4102444800})
        return httpx.Response(200, json={"ok": True})

    return httpx.MockTransport(handler)


def _state(db_session, tmp_path: Path, test_settings: Settings) -> SimpleNamespace:
    fake_file = tmp_path / "scenario.json"
    events = [
        {"kind": "delta", "text": "分析中"},
        {"kind": "subagent", "text": "PROBE_OK 探查成功", "data": {}},
        {
            "kind": "result",
            "text": json.dumps(
                {
                    "root_cause_category": "FW_DEFECT",
                    "root_cause_detail": "固件缺陷：超时路径缺少重试",
                    "evidence": [],
                    "solution": "升级固件",
                    "gap_note": None,
                },
                ensure_ascii=False,
            ),
        },
    ]
    fake_file.write_text(json.dumps({"events": events}, ensure_ascii=False), encoding="utf-8")
    settings = test_settings.model_copy(
        update={
            "ai": AISettings(edition="fake", fake_scenario_file=fake_file),
            "aitest": AitestSettings(base_url="http://mock", read_token=SecretStr("read-t")),
        }
    )
    return SimpleNamespace(
        settings=settings,
        clock=SystemClock(),
        ids=UuidIdGenerator(),
        cleaning_registry=CleaningRegistry(),
        cutoff_rules=RuleSet.load(Path("deploy/config/rules/cutoff.yaml")),
        ai_worker=build_ai_worker(
            settings, ids=UuidIdGenerator(), token_transport=_mock_transport()
        ),
        callback_worker=CallbackWorker(
            client_factory=lambda: httpx.Client(transport=_mock_transport()),
            base_url="http://mock",
            read_token="read-t",
        ),
    )


def test_run_tick_drives_full_chain(db_session, tmp_path: Path, test_settings: Settings) -> None:
    for name in ("test", "uart", "dmesg", "scheduler"):
        (tmp_path / f"{name}.txt").write_text(
            {
                "test": "2026-11-20T10:53:43.500Z ERROR AssertionError: gpf err_code=0x10B\n",
                "uart": "Build ID: X\n",
                "dmesg": "",
                "scheduler": "2026-11-20T10:00:00Z STATE EXIT\n",
            }[name],
            encoding="utf-8",
        )
    session_id = _deliver(db_session, tmp_path)
    state = _state(db_session, tmp_path, test_settings)
    for _ in range(8):
        run_tick(db_session, state, "w1")
        db_session.commit()
    task = db_session.execute(
        select(TaskQueue).where(
            TaskQueue.session_id == session_id, TaskQueue.task_type == TaskType.CASE_AI.value
        )
    ).scalar_one()
    assert task.queue_state == QueueState.DONE.value
    round_row = db_session.execute(select(Round).where(Round.session_id == session_id)).scalar_one()
    assert round_row.stage == Stage.COMPLETED.value
    report = db_session.get(Report, round_row.report_id)
    assert report is not None
    assert report.result["root_cause"]["category"] == "FW_DEFECT"
    callbacks = (
        db_session.execute(select(TaskQueue).where(TaskQueue.task_type == TaskType.CALLBACK.value))
        .scalars()
        .all()
    )
    assert callbacks
    assert any(c.callback_status == "SENT" for c in callbacks)


def test_build_ai_worker_fake_requires_scenario_file(test_settings: Settings) -> None:
    import pytest

    from aitrace.core.settings import SettingsError

    settings = test_settings.model_copy(
        update={"ai": AISettings(edition="fake", fake_scenario_file=None)}
    )
    with pytest.raises(SettingsError, match="fake_scenario_file"):
        build_ai_worker(settings, ids=UuidIdGenerator())
