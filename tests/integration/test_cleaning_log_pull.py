from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import httpx
from sqlalchemy import select

from aitrace.api.schemas import DeliveryPayload
from aitrace.cleaning.registry import CleaningRegistry
from aitrace.cleaning.rules import RuleSet
from aitrace.cleaning.worker import CleaningWorker
from aitrace.core.enums import QueueState, Stage
from aitrace.core.ids import SystemClock, UuidIdGenerator
from aitrace.ingest.log_client import LogClient
from aitrace.ingest.service import IngestService
from aitrace.persistence.models import Report, Round, TaskQueue
from aitrace.persistence.repositories.queue import QueueRepository
from aitrace.profiles.loader import load_profiles
from aitrace.profiles.resolver import ProfileResolver
from tests.fixtures.logs import load_golden

BATCH_ID = "BATCH-20261120-0089"
LOGS = {
    "test_log_path": "var/log/aitest/CASE-pull/test.txt",
    "uart_log_path": "var/log/aitest/CASE-pull/uart.txt",
    "kernel_log_path": "var/log/aitest/CASE-pull/dmesg.txt",
    "scheduler_log_path": "var/log/aitest/CASE-pull/scheduler.txt",
}


def _delivery(trigger_user_id: str = "zhangsan") -> DeliveryPayload:
    return DeliveryPayload.model_validate(
        {
            "product_id": "ssd_9560",
            "batch_id": BATCH_ID,
            "trigger_user_id": trigger_user_id,
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
                    "case_id": "CASE-pull",
                    "script_path": "/cases/power_down_resume.py",
                    "failed_at_utc": 1768877624000,
                    "env_binding": {"device_id": "nvme0n1"},
                    "logs": dict(LOGS),
                }
            ],
            "passed_cases": [],
        }
    )


def _setup(db_session, trigger_user_id: str = "zhangsan") -> str:
    registry = load_profiles(Path("deploy/config/domains"))
    resolver = ProfileResolver(registry)
    outcome = IngestService(db_session, resolver, SystemClock(), UuidIdGenerator()).deliver(
        _delivery(trigger_user_id)
    )
    db_session.flush()
    return outcome.response.sessions[0].session_id


def _pull_client(sources: dict[str, str]) -> LogClient:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.params["path"]
        if path not in sources:
            return httpx.Response(404, json={"code": "LOG_NOT_FOUND", "message": "日志不存在"})
        return httpx.Response(200, content=sources[path].encode("utf-8"))

    return LogClient(
        base_url="http://aitest.test",
        read_token="mock-read-token",
        transport=httpx.MockTransport(handler),
    )


def _worker(db_session, data_dir: Path, now: datetime, log_client: LogClient) -> int:
    rules = RuleSet.load(Path("deploy/config/rules/cutoff.yaml"))
    worker = CleaningWorker(
        QueueRepository(db_session), CleaningRegistry(), rules, log_client=log_client
    )
    return worker.run_once(
        db_session, clock=FrozenClock(now), ids=UuidIdGenerator(), data_dir=data_dir, owner="w1"
    )


class FrozenClock:
    def __init__(self, now: datetime) -> None:
        self._now = now

    def now(self) -> datetime:
        return self._now


def test_pull_missing_logs_from_aitest_then_clean(db_session, tmp_path: Path) -> None:
    # 本地 L2 无任何日志：清洗 worker 必须经 aitest 日志接口拉全四路后正常出规则报告
    sources = {
        path: load_golden(name)
        for name, path in (
            ("test", "var/log/aitest/CASE-pull/test.txt"),
            ("uart", "var/log/aitest/CASE-pull/uart.txt"),
            ("dmesg", "var/log/aitest/CASE-pull/dmesg.txt"),
            ("scheduler", "var/log/aitest/CASE-pull/scheduler.txt"),
        )
    }
    session_id = _setup(db_session)
    now = datetime(2026, 11, 27, 8, 0, 0, tzinfo=UTC)
    handled = _worker(db_session, tmp_path, now, _pull_client(sources))
    assert handled == 1
    task = db_session.execute(
        select(TaskQueue).where(TaskQueue.session_id == session_id)
    ).scalar_one()
    assert task.queue_state == QueueState.DONE.value
    round_row = db_session.execute(select(Round).where(Round.session_id == session_id)).scalar_one()
    assert round_row.stage == Stage.COMPLETED.value
    report = db_session.get(Report, round_row.report_id)
    assert report.result["root_cause"]["category"] == "TEST_ENV_ERROR"
    raw_root = tmp_path / "rawlogs" / BATCH_ID
    for path in LOGS.values():
        assert (raw_root / path).is_file()
    assert "log_pull_errors" not in (task.payload or {})


def test_pull_single_source_missing_marks_gap_not_fail(db_session, tmp_path: Path) -> None:
    # 三路可拉、kernel 缺失：只标注缺口，清洗继续（不整体失败）
    sources = {
        "var/log/aitest/CASE-pull/test.txt": (
            "2026-11-20T10:00:00Z INFO ok\n"
            "2026-11-20T10:53:43.500Z ERROR AssertionError: gpf err_code=0x10B\n"
        ),
        "var/log/aitest/CASE-pull/uart.txt": "Build ID: X\n",
        "var/log/aitest/CASE-pull/scheduler.txt": "2026-11-20T10:00:00Z STATE EXIT\n",
    }
    session_id = _setup(db_session)
    now = datetime(2026, 11, 27, 8, 0, 0, tzinfo=UTC)
    handled = _worker(db_session, tmp_path, now, _pull_client(sources))
    assert handled == 1
    task = db_session.execute(
        select(TaskQueue).where(TaskQueue.session_id == session_id)
    ).scalar_one()
    assert task.queue_state == QueueState.READY.value
    errors = (task.payload or {}).get("log_pull_errors", {})
    assert errors["kernel_log_path"]["code"] == "LOG_NOT_FOUND"
    round_row = db_session.execute(select(Round).where(Round.session_id == session_id)).scalar_one()
    assert round_row.exact_fingerprint is not None
