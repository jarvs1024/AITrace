from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select

from aitrace.api.schemas import DeliveryPayload
from aitrace.cleaning.registry import CleaningRegistry
from aitrace.cleaning.rules import RuleSet
from aitrace.cleaning.worker import CleaningWorker
from aitrace.core.enums import QueueState, Stage
from aitrace.core.ids import SystemClock, UuidIdGenerator
from aitrace.ingest.service import IngestService
from aitrace.persistence.models import Report, Round, SessionModel, TaskQueue
from aitrace.persistence.repositories.queue import QueueRepository
from aitrace.profiles.loader import load_profiles
from aitrace.profiles.resolver import ProfileResolver
from tests.fixtures.logs import load_golden


def _setup(db_session, data_dir: Path, trigger_user_id: str = "zhangsan") -> str:
    # 用 M0 ingest 的方式构造 PENDING case_clean 任务
    payload = DeliveryPayload.model_validate(
        {
            "product_id": "ssd_9560",
            "batch_id": "BATCH-20261120-0089",
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
                    "case_id": f"CASE-{trigger_user_id}",
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


def _worker(db_session, data_dir: Path, now: datetime) -> int:
    rules = RuleSet.load(Path("deploy/config/rules/cutoff.yaml"))
    worker = CleaningWorker(QueueRepository(db_session), CleaningRegistry(), rules)
    return worker.run_once(
        db_session, clock=FrozenClock(now), ids=UuidIdGenerator(), data_dir=data_dir, owner="w1"
    )


class FrozenClock:
    def __init__(self, now: datetime) -> None:
        self._now = now

    def now(self) -> datetime:
        return self._now


def test_rule_hit_flow_writes_report_and_finishes_done(db_session, tmp_path: Path) -> None:
    for name in ("test", "uart", "dmesg", "scheduler"):
        (tmp_path / f"{name}.txt").write_text(load_golden(name), encoding="utf-8")
    session_id = _setup(db_session, tmp_path)
    now = datetime(2026, 11, 26, 8, 0, 0, tzinfo=UTC)
    handled = _worker(db_session, tmp_path, now)
    assert handled == 1
    task = db_session.execute(
        select(TaskQueue).where(
            TaskQueue.session_id == session_id,
            TaskQueue.task_type == "case_clean",
        )
    ).scalar_one()
    assert task.queue_state == QueueState.DONE.value
    round_row = db_session.execute(select(Round).where(Round.session_id == session_id)).scalar_one()
    assert round_row.stage == Stage.COMPLETED.value
    assert round_row.report_id is not None
    report = db_session.get(Report, round_row.report_id)
    assert report.result["root_cause"]["category"] == "TEST_ENV_ERROR"
    assert report.citation == {"citing_status": "NONE"}


def test_rule_hit_flow_emits_result_callback_and_notify(db_session, tmp_path: Path) -> None:
    # 规格 §4.5.4：终态结果必发——规则命中轮次同样必须产生 RESULT 回调与案例通知
    for name in ("test", "uart", "dmesg", "scheduler"):
        (tmp_path / f"{name}.txt").write_text(load_golden(name), encoding="utf-8")
    session_id = _setup(db_session, tmp_path)
    now = datetime(2026, 11, 26, 8, 0, 0, tzinfo=UTC)
    handled = _worker(db_session, tmp_path, now)
    assert handled == 1
    callbacks = (
        db_session.execute(
            select(TaskQueue).where(
                TaskQueue.session_id == session_id,
                TaskQueue.task_type == "callback",
            )
        )
        .scalars()
        .all()
    )
    result_payloads = [c.payload for c in callbacks if c.payload.get("event") == "RESULT"]
    assert len(result_payloads) == 1
    payload = result_payloads[0]
    assert payload["report"]["root_cause"]["category"] == "TEST_ENV_ERROR"
    assert payload["report"]["confidence"] is None
    assert payload["citation"] == {"citing_status": "NONE"}
    assert payload["stage"] == "COMPLETED"
    assert payload["progress"] == 100
    notifies = (
        db_session.execute(
            select(TaskQueue).where(
                TaskQueue.session_id == session_id,
                TaskQueue.task_type == "notify",
            )
        )
        .scalars()
        .all()
    )
    assert len(notifies) == 1
    assert notifies[0].payload["event"] == "batch_card"
    assert notifies[0].payload["category"] == "TEST_ENV_ERROR"


def test_ready_flow_finishes_ready_without_report(db_session, tmp_path: Path) -> None:
    # 无规则命中的日志 → READY，等待 M2 AI
    (tmp_path / "test.txt").write_text(
        "2026-11-20T10:00:00Z INFO ok\n"
        "2026-11-20T10:53:43.500Z ERROR AssertionError: gpf err_code=0x10B\n",
        encoding="utf-8",
    )
    (tmp_path / "uart.txt").write_text("Build ID: X\n", encoding="utf-8")
    (tmp_path / "dmesg.txt").write_text("", encoding="utf-8")
    (tmp_path / "scheduler.txt").write_text("2026-11-20T10:00:00Z STATE EXIT\n", encoding="utf-8")
    session_id = _setup(db_session, tmp_path, trigger_user_id="lisi")
    now = datetime(2026, 11, 26, 8, 0, 0, tzinfo=UTC)
    handled = _worker(db_session, tmp_path, now)
    assert handled == 1
    task = db_session.execute(
        select(TaskQueue).where(TaskQueue.session_id == session_id)
    ).scalar_one()
    assert task.queue_state == QueueState.READY.value
    round_row = db_session.execute(select(Round).where(Round.session_id == session_id)).scalar_one()
    assert round_row.stage == Stage.CLEANING.value  # 清洗完成等待 AI 的阶段语义
    assert round_row.exact_fingerprint is not None
    session_row = db_session.execute(
        select(SessionModel).where(SessionModel.session_id == session_id)
    ).scalar_one()
    assert session_row.stable_fingerprint is not None
    assert (tmp_path / "evidence" / session_id / "round_1" / "summary.json").is_file()
