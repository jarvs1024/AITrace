from datetime import UTC, datetime
from pathlib import Path

from aitrace.cleaning.pipeline import PipelineOutcome, run_pipeline
from aitrace.cleaning.registry import CleaningRegistry
from aitrace.cleaning.rules import RuleSet
from tests.fixtures.logs import load_golden

BOOT = datetime(2026, 11, 20, 2, 0, 0, tzinfo=UTC)
FAILED_AT = int(datetime(2026, 11, 20, 10, 53, 44, tzinfo=UTC).timestamp() * 1000)

PAYLOAD = {
    "script_path": "/cases/power_down_resume.py",
    "failed_at_utc": FAILED_AT,
    "env_binding": {
        "device_id": "nvme0n1",
        "sn": "SN-9560-001",
        "slot": "3",
        "fw_version": "FW-2.4.1",
    },
    "logs": {
        "test_log_path": "test.txt",
        "uart_log_path": "uart.txt",
        "kernel_log_path": "dmesg.txt",
        "scheduler_log_path": "scheduler.txt",
    },
}
AUTHORITATIVE = {
    "product_id": "ssd_9560",
    "batch_id": "BATCH-20261120-0089",
    "case_id": "CASE-1021",
    "trigger_user_id": "zhangsan",
    "hardware_type": "SSD",
    "env_topology": {
        "host_ip": "10.20.68.114",
        "host_boot_time_utc": int(BOOT.timestamp()),
        "devices": [],
    },
    "git_context": {"repo_url": "git@x:y.git", "commit_id": "abc123"},
}

# 预检裁决 2：golden scheduler 含“锁板冲突”、golden dmesg 含“PCIe Link Down”，
# 全量 golden 必然命中截流闸；ready 路径测试用净化后的两条日志覆盖。
_SANITIZED_SCHEDULER = "2026-11-20T10:00:00Z STATE EXIT\n"
_SANITIZED_DMESG = "[  300.000000] nvme nvme0: device enumerated sn=SN-9560-001\n"


def _run(
    tmp_path: Path,
    payload: dict | None = None,
    *,
    sanitize: bool = False,
    drop: str | None = None,
    write_goldens: bool = True,
) -> PipelineOutcome:
    if write_goldens:
        for name in ("test", "uart", "dmesg", "scheduler"):
            (tmp_path / f"{name}.txt").write_text(load_golden(name), encoding="utf-8")
    if sanitize:
        (tmp_path / "scheduler.txt").write_text(_SANITIZED_SCHEDULER, encoding="utf-8")
        (tmp_path / "dmesg.txt").write_text(_SANITIZED_DMESG, encoding="utf-8")
    if drop is not None:
        (tmp_path / f"{drop}.txt").unlink(missing_ok=True)
    rules = RuleSet.load(Path("deploy/config/rules/cutoff.yaml"))
    return run_pipeline(
        payload=payload or PAYLOAD,
        authoritative=AUTHORITATIVE,
        raw_root=tmp_path,
        profile=None,
        rules=rules,
        registry=CleaningRegistry(),
    )


def test_ready_pipeline_produces_fingerprints_and_anchor(tmp_path: Path) -> None:
    outcome = _run(tmp_path, sanitize=True)
    assert outcome.status == "ready"
    assert outcome.first_error is not None
    assert outcome.anchor is not None
    assert outcome.stable_fp and len(outcome.stable_fp) == 16
    assert outcome.exact_fp and len(outcome.exact_fp) == 16
    assert outcome.summary["anchor_utc"] is not None
    assert outcome.metrics["alignment_rate"] == 1.0


def test_missing_logs_marked_unavailable(tmp_path: Path) -> None:
    outcome = _run(tmp_path, sanitize=True, drop="test")
    assert outcome.status == "ready"
    assert outcome.summary["source_availability"]["test"] is False
    assert outcome.metrics["alignment_rate"] < 1.0


def test_rule_hit_short_circuits(tmp_path: Path) -> None:
    outcome = _run(tmp_path, PAYLOAD)
    # golden scheduler 含“锁板冲突”→ 截流闸命中；全量 golden 保留以验证短路
    assert outcome.status == "rule_hit"
    assert outcome.rule_hit is not None
    assert outcome.rule_hit.category == "TEST_ENV_ERROR"
    assert outcome.rule_hit.rule_id == "lock_conflict"


def test_all_logs_missing_fails(tmp_path: Path) -> None:
    outcome = _run(tmp_path, write_goldens=False)
    assert outcome.status == "failed"
    assert outcome.error is not None
    assert "日志" in outcome.error["message"]
