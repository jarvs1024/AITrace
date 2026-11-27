#!/usr/bin/env python3
"""AITrace SLO 标定 harness（make perf）。

拉起 aitrace（fake AI 链）+ mock aitest 子进程，自行投递批次并采集：
受理确认、排队转清洗、令牌队列分桶等待、跨用户公平性、AI 时长、
单 case 端到端分桶、批次完成、背压（429 QUEUE_FULL）与回调风暴。
输出 perf_report.json 并打印中文 SLO 对照摘要。

纯函数 percentile / bucket_waits / fairness_gap / summarize 由
tests/perf/test_perf_math.py 单测约束。
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import create_engine, text

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB_URL = "postgresql+psycopg://aitrace:aitrace@127.0.0.1:5432/aitrace_test"
READ_TOKEN = "mock-read-token"
SERVICE_TOKEN = "dev-service-token"

_TRUNCATE = """
TRUNCATE TABLE audit_log, attachment, callback_seq, profile, memory_hot_file,
case_contribution, case_library, user_queue, task_queue, batch, report, "round", "session"
RESTART IDENTITY CASCADE
"""


# 四路日志内容刻意避开截流闸规则，保证清洗不短路、必然进入 AI 阶段。
def _case_logs(idx: int) -> dict[str, str]:
    """err_code 按用例序号互异 → 稳定指纹互异，避免整批同指纹触发多数派升级。"""
    return {
        "test.log": (
            "2026-11-20T10:00:02.000Z CMD nvme power-down @CMD uart_tick=12345\n"
            "2026-11-20T10:53:43.500Z ERROR TimeoutException: uart no response "
            f"err_code=0x1A{idx}\n"
        ),
        "uart.log": "Build ID: X\n@TICK 12345\n",
        "dmesg.log": "2026-11-20T10:00:00.000Z kernel: nvme0n1: probe ok\n",
        "scheduler.log": "2026-11-20T10:00:00Z STATE RUNNING\n2026-11-20T10:55:00Z STATE EXIT\n",
    }


_LOG_CONFIG = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {"plain": {"format": "%(message)s"}},
    "handlers": {"console": {"class": "logging.StreamHandler", "formatter": "plain"}},
    "loggers": {
        "uvicorn": {"level": "WARNING", "handlers": ["console"]},
        "uvicorn.error": {"level": "WARNING", "handlers": ["console"]},
        "uvicorn.access": {"level": "WARNING"},
    },
    "root": {"level": "INFO", "handlers": ["console"]},
}


# ---------------------------------------------------------------- 纯函数块


def percentile(values: list[float], q: float) -> float:
    """最邻近等级法分位（与 aitrace.observability.metrics.Histogram 同口径）。"""
    if not values:
        raise ValueError("percentile() 不接受空样本")
    ordered = sorted(values)
    n = len(ordered)
    index = min(n, max(1, int(q / 100.0 * n + 0.999)))
    return ordered[index - 1]


def depth_bucket(depth: int) -> str:
    if depth <= 0:
        return "0"
    if depth <= 2:
        return "1-2"
    if depth <= 4:
        return "3-4"
    return ">4"


def _stats(values: list[float]) -> dict[str, float | int | None]:
    if not values:
        return {"count": 0, "p50": None, "p95": None, "p99": None}
    return {
        "count": len(values),
        "p50": round(percentile(values, 50), 3),
        "p95": round(percentile(values, 95), 3),
        "p99": round(percentile(values, 99), 3),
    }


def bucket_waits(waits: list[tuple[str, int, float]]) -> dict[str, dict[str, Any]]:
    """list[(queue_key, depth, wait_seconds)] → {"0"/"1-2"/"3-4"/">4": 统计}。"""
    by_bucket: dict[str, list[float]] = {}
    for _, depth, seconds in waits:
        by_bucket.setdefault(depth_bucket(depth), []).append(seconds)
    return {name: _stats(by_bucket.get(name, [])) for name in ("0", "1-2", "3-4", ">4")}


def fairness_gap(buckets: dict[tuple[str, str], list[float]], *, bucket: str) -> float:
    """同一前序深度组内各 queue_key 的 P95 等待最大值减最小值；单键或无样本为 0。"""
    p95s = [percentile(v, 95) for (_, b), v in buckets.items() if b == bucket and v]
    if len(p95s) < 2:
        return 0.0
    return round(max(p95s) - min(p95s), 3)


@dataclass(frozen=True)
class SloSettings:
    """§7.1 初始目标（阶段二起始参考值，D=AI 任务 P95 时长初始 6min）。"""

    accept_p95_seconds: float = 5.0
    queue_to_clean_p95_seconds: float = 120.0
    clean_seconds_per_gb: float = 60.0
    ai_task_p95_seconds: float = 360.0
    queue_wait_bucket0_p95_seconds: float = 60.0
    queue_wait_bucket1to4_factor: float = 4.0
    fairness_gap_factor: float = 2.0
    e2e_bucket0_p95_seconds: float = 900.0
    e2e_bucket1to4_p95_seconds: float = 2700.0
    batch_p95_seconds: float = 3600.0

    @property
    def queue_wait_bucket1to4_p95_seconds(self) -> float:
        return self.queue_wait_bucket1to4_factor * self.ai_task_p95_seconds

    @property
    def fairness_gap_seconds(self) -> float:
        return self.fairness_gap_factor * self.ai_task_p95_seconds


@dataclass(frozen=True)
class SloResult:
    metric: str
    target: str
    measured: float | None
    ok: bool


def _buckets_p95(buckets: dict[str, dict[str, Any]], names: tuple[str, ...]) -> float | None:
    found: list[float] = []
    for name in names:
        bucket = buckets.get(name)
        if bucket and bucket.get("p95") is not None:
            found.append(float(bucket["p95"]))
    return max(found) if found else None


def summarize(metrics: dict[str, Any], slo: SloSettings) -> list[SloResult]:
    """对照 §7.1 目标输出判定；样本缺失按实测 None（未达标，不虚构）。"""
    results: list[SloResult] = []

    def add(metric: str, target: str, measured: float | None, limit: float | None = None) -> None:
        ok = measured is not None and (limit is None or measured <= limit)
        results.append(SloResult(metric, target, measured, ok))

    accept = metrics.get("accept")
    accept_p95 = accept.get("p95") if isinstance(accept, dict) else None
    add("投递受理确认 P95", f"< {slo.accept_p95_seconds:g}s", accept_p95, slo.accept_p95_seconds)

    q2c = metrics.get("queue_to_clean_p95")
    add(
        "排队转清洗 P95",
        f"< {slo.queue_to_clean_p95_seconds:g}s（全局未饱和）",
        q2c,
        slo.queue_to_clean_p95_seconds,
    )

    waits = metrics.get("queue_wait")
    waits = waits if isinstance(waits, dict) else {}
    add(
        "令牌队列等待 P95（前序 0）",
        f"< {slo.queue_wait_bucket0_p95_seconds:g}s",
        _buckets_p95(waits, ("0",)),
        slo.queue_wait_bucket0_p95_seconds,
    )
    add(
        "令牌队列等待 P95（前序 1-4）",
        f"≤ 4 倍 D = {slo.queue_wait_bucket1to4_p95_seconds:g}s",
        _buckets_p95(waits, ("1-2", "3-4")),
        slo.queue_wait_bucket1to4_p95_seconds,
    )

    fairness = metrics.get("fairness")
    fairness = fairness if isinstance(fairness, dict) else {}
    if fairness:
        for bucket, label in (("0", "前序 0"), ("1-2", "前序 1-2"), ("3-4", "前序 3-4")):
            gap = fairness.get(bucket)
            add(
                f"跨用户公平性（{label}）",
                f"≤ 2 倍 D = {slo.fairness_gap_seconds:g}s",
                gap if isinstance(gap, (int, float)) else None,
                slo.fairness_gap_seconds,
            )

    e2e = metrics.get("e2e")
    e2e = e2e if isinstance(e2e, dict) else {}
    add(
        "单 case 端到端 P95（前序 0）",
        f"< {slo.e2e_bucket0_p95_seconds:g}s",
        _buckets_p95(e2e, ("0",)),
        slo.e2e_bucket0_p95_seconds,
    )
    add(
        "单 case 端到端 P95（前序 1-4）",
        f"< {slo.e2e_bucket1to4_p95_seconds:g}s",
        _buckets_p95(e2e, ("1-4",)),
        slo.e2e_bucket1to4_p95_seconds,
    )

    add(
        "批次完成 P95",
        f"< {slo.batch_p95_seconds:g}s",
        metrics.get("batch_p95"),
        slo.batch_p95_seconds,
    )

    callbacks = metrics.get("callbacks")
    if isinstance(callbacks, dict):
        results.append(
            SloResult(
                "回调风暴（送达）",
                "回调总量 ≥ 会话数且 ≥ 1",
                float(callbacks.get("total", 0) or 0),
                bool(callbacks.get("total"))
                and callbacks.get("total", 0) >= callbacks.get("sessions", 0),
            )
        )
    else:
        add("回调风暴（送达）", "回调总量 ≥ 会话数且 ≥ 1", None)
    return results


# ---------------------------------------------------------------- 进程与设施


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _stop(process: subprocess.Popen[str] | None) -> None:
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=10)


def _start_process(
    command: list[str],
    ready_url: str,
    *,
    env: dict[str, str],
    lines: list[str],
    ready_timeout: float = 20.0,
) -> subprocess.Popen[str]:
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        env=env,
        cwd=ROOT,
    )
    threading.Thread(target=_drain, args=(process, lines), daemon=True).start()
    deadline = time.monotonic() + ready_timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            tail = "".join(lines[-2000:])
            raise RuntimeError(f"服务提前退出（rc={process.returncode}）：{ready_url}\n{tail}")
        try:
            if httpx.get(ready_url, timeout=0.5, trust_env=False).status_code == 200:
                return process
        except httpx.HTTPError:
            pass
        time.sleep(0.1)
    raise RuntimeError(f"服务在 {ready_timeout}s 内未就绪：{ready_url}")


def _drain(process: subprocess.Popen[str], lines: list[str]) -> None:
    assert process.stdout is not None
    for line in process.stdout:
        lines.append(line)


def _truncate_all(db_url: str) -> None:
    engine = create_engine(db_url)
    try:
        with engine.begin() as conn:
            conn.execute(text(_TRUNCATE))
    finally:
        engine.dispose()


def _migrate(db_url: str) -> None:
    subprocess.run(
        [sys.executable, "-m", "alembic", "-x", f"db_url={db_url}", "upgrade", "head"],
        check=True,
        cwd=ROOT,
        capture_output=True,
        text=True,
    )


def _batch_state(db_url: str, batch_ids: list[str]) -> dict[str, str]:
    engine = create_engine(db_url)
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                text("SELECT batch_id, batch_state FROM batch WHERE batch_id = ANY(:ids)"),
                {"ids": batch_ids},
            ).all()
        return {row[0]: row[1] for row in rows}
    finally:
        engine.dispose()


def _wait_batches(db_url: str, batch_ids: list[str], timeout: float) -> dict[str, float]:
    started = time.monotonic()
    done: dict[str, float] = {}
    deadline = started + timeout
    while time.monotonic() < deadline:
        states = _batch_state(db_url, batch_ids)
        for bid, state in states.items():
            if state in {"COMPLETED", "FAILED"} and bid not in done:
                done[bid] = time.monotonic() - started
        if len(done) == len(batch_ids):
            return done
        time.sleep(0.5)
    missing = [bid for bid in batch_ids if bid not in done]
    raise RuntimeError(f"等待批次完成超时（{timeout}s）：未终态 {missing}")


def _callbacks(mock_url: str) -> list[dict[str, Any]]:
    resp = httpx.get(
        f"{mock_url}/__mock/callbacks",
        headers={"X-Aitest-Read-Token": READ_TOKEN},
        timeout=5.0,
        trust_env=False,
    )
    resp.raise_for_status()
    return resp.json()["items"]


def _callback_metrics(mock_url: str, sessions: int) -> dict[str, Any]:
    items = _callbacks(mock_url)
    by_event: dict[str, int] = {}
    result_sessions: set[str] = set()
    for item in items:
        by_event[item.get("event", "?")] = by_event.get(item.get("event", "?"), 0) + 1
        if item.get("event") == "RESULT" and item.get("session_id"):
            result_sessions.add(item["session_id"])
    return {
        "total": len(items),
        "by_event": by_event,
        "sessions": sessions,
        "sessions_with_result": len(result_sessions),
    }


def _seed_logs(data_dir: Path, batch_id: str, case_ids: list[str]) -> None:
    for idx, case_id in enumerate(case_ids):
        for name, content in _case_logs(idx).items():
            path = data_dir / "rawlogs" / batch_id / "var/log/aitest" / case_id / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")


def _delivery_payload(batch_id: str, user: str, case_ids: list[str]) -> dict[str, Any]:
    return {
        "product_id": "ssd_9560",
        "batch_id": batch_id,
        "trigger_user_id": user,
        "hardware_type": "SSD",
        "env_topology": {
            "host_ip": "10.23.45.67",
            "host_boot_time_utc": 1763000000,
            "devices": [
                {
                    "device_id": "nvme-ssd-001",
                    "pcie_bdf": "0000:01:00.0",
                    "device_node": "/dev/nvme0n1",
                    "model": "SSD-9560",
                    "sn": f"SN-PERF-{batch_id[-6:]}",
                    "fw_version": "2.3.1",
                    "slot": "slot-0",
                }
            ],
        },
        "git_context": {
            "repo_url": "https://git.example.com/ssd/aitest-cases.git",
            "commit_id": "abc123def4567890123456789012345678901234",
        },
        "failed_cases": [
            {
                "case_id": case_id,
                "script_path": "cases/perf/random_write_72h.py",
                "failed_at_utc": 1763100000,
                "env_binding": {"device_id": "nvme-ssd-001"},
                "logs": {
                    "test_log_path": f"var/log/aitest/{case_id}/test.log",
                    "uart_log_path": f"var/log/aitest/{case_id}/uart.log",
                    "kernel_log_path": f"var/log/aitest/{case_id}/dmesg.log",
                    "scheduler_log_path": f"var/log/aitest/{case_id}/scheduler.log",
                },
            }
            for case_id in case_ids
        ],
    }


def _stage_events(lines: list[str]) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for line in lines:
        try:
            payload = json.loads(line)
        except ValueError:
            continue
        if payload.get("event") == "task_stage":
            events.append(payload)
    return events


def _queue_waits_from_events(events: list[dict[str, Any]]) -> list[tuple[str, int, float]]:
    per_key: dict[str, list[float]] = {}
    for event in events:
        if event.get("from_state") == "READY" and event.get("to_state") == "AI_RUNNING":
            per_key.setdefault(str(event["queue_key"]), []).append(float(event["duration_seconds"]))
    waits: list[tuple[str, int, float]] = []
    for key, durations in per_key.items():
        waits.extend((key, depth, round(d, 3)) for depth, d in enumerate(durations))
    return waits


def _durations(
    events: list[dict[str, Any]],
    *,
    to_state: str | None = None,
    from_state: str | None = None,
) -> list[float]:
    out: list[float] = []
    for event in events:
        if to_state is not None and event.get("to_state") != to_state:
            continue
        if from_state is not None and event.get("from_state") != from_state:
            continue
        out.append(float(event["duration_seconds"]))
    return out


def _e2e_buckets(db_url: str, batch_ids: list[str]) -> dict[str, dict[str, Any]]:
    engine = create_engine(db_url)
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT t.queue_key, t.created_at, r.finished_at FROM task_queue t "
                    'JOIN "round" r ON r.session_id = t.session_id AND r.round_no = t.round_no '
                    "WHERE t.task_type = 'case_ai' AND r.finished_at IS NOT NULL "
                    "AND t.batch_id = ANY(:ids) ORDER BY t.queue_key, t.created_at"
                ),
                {"ids": batch_ids},
            ).all()
    finally:
        engine.dispose()
    per_key: dict[str, list[float]] = {}
    for queue_key, created, finished in rows:
        duration = (finished - created).total_seconds()
        per_key.setdefault(str(queue_key), []).append(duration)
    bucket0: list[float] = []
    bucket1to4: list[float] = []
    for durations in per_key.values():
        for depth, duration in enumerate(durations):
            (bucket0 if depth == 0 else bucket1to4).append(duration)
    return {"0": _stats(bucket0), "1-4": _stats(bucket1to4)}


@dataclass
class Ctx:
    tmp: Path
    db_url: str
    ai_workers: int
    app_lines: list[str]
    data_dir: Path
    aitrace_url: str = ""
    mock_url: str = ""
    aitrace_proc: subprocess.Popen[str] | None = None
    mock_proc: subprocess.Popen[str] | None = None


def _app_env(ctx: Ctx, extra: dict[str, str] | None = None) -> dict[str, str]:
    env = {
        **os.environ,
        "AITRACE_SERVICE_TOKEN": SERVICE_TOKEN,
        "AITRACE_DATABASE__DSN": ctx.db_url,
        "AITRACE_AI__EDITION": "fake",
        "AITRACE_AI__FAKE_SCENARIO_FILE": str(ROOT / "tests/fixtures/ai_fake_scenario.json"),
        "AITRACE_AI__PAUSE_BACKOFF_BASE_SECONDS": "1",
        "AITRACE_STORAGE__DATA_DIR": str(ctx.data_dir),
        "AITRACE_STORAGE__PROFILE_DOMAINS_DIR": str(ROOT / "deploy/config/domains"),
        "AITRACE_STORAGE__CUTOFF_RULES_FILE": str(ROOT / "deploy/config/rules/cutoff.yaml"),
        "AITRACE_AITEST__BASE_URL": ctx.mock_url,
        "AITRACE_AITEST__READ_TOKEN": READ_TOKEN,
        "AITRACE_QUEUE__AI_WORKER_CONCURRENCY": str(ctx.ai_workers),
    }
    if extra:
        env.update(extra)
    return env


def _start_services(ctx: Ctx, extra_app_env: dict[str, str] | None = None) -> None:
    log_config = ctx.tmp / "uvicorn-log.json"
    log_config.write_text(json.dumps(_LOG_CONFIG), encoding="utf-8")
    aitrace_port = _free_port()
    mock_port = _free_port()
    ctx.mock_url = f"http://127.0.0.1:{mock_port}"
    ctx.aitrace_proc = _start_process(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "aitrace.api.app:create_app",
            "--factory",
            "--host",
            "127.0.0.1",
            "--port",
            str(aitrace_port),
            "--log-config",
            str(log_config),
        ],
        f"http://127.0.0.1:{aitrace_port}/api/v1/health",
        env=_app_env(ctx, extra_app_env),
        lines=ctx.app_lines,
    )
    ctx.aitrace_url = f"http://127.0.0.1:{aitrace_port}"
    ctx.mock_proc = _start_process(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "aitest_mock.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(mock_port),
            "--log-level",
            "warning",
        ],
        f"http://127.0.0.1:{mock_port}/api/v1/health",
        env={
            **os.environ,
            "AITRACE_BASE_URL": ctx.aitrace_url,
            "AITRACE_SERVICE_TOKEN": SERVICE_TOKEN,
            "AITEST_READ_TOKEN": READ_TOKEN,
            "AITEST_MOCK_WEBHOOK_BASE": ctx.mock_url,
        },
        lines=[],
    )


def _stop_services(ctx: Ctx) -> None:
    _stop(ctx.mock_proc)
    _stop(ctx.aitrace_proc)


def _deliver(ctx: Ctx, payload: dict[str, Any]) -> tuple[float, int, dict[str, Any]]:
    """返回 (受理 RTT 秒, 状态码, 响应体)；受理口径与 §7.1 秒级目标一致。"""
    headers = {"Authorization": f"Bearer {SERVICE_TOKEN}"}
    started = time.perf_counter()
    resp = httpx.post(
        f"{ctx.aitrace_url}/api/v1/aitrace/tasks",
        json=payload,
        headers=headers,
        timeout=30.0,
        trust_env=False,
    )
    rtt = time.perf_counter() - started
    try:
        body: dict[str, Any] = resp.json()
    except ValueError:
        body = {}
    return rtt, resp.status_code, body


def _new_batch_id() -> str:
    return f"PERF-{uuid.uuid4().hex[:8]}"


# ---------------------------------------------------------------- Profiles


def run_burndown(ctx: Ctx, batches: int, timeout: float) -> dict[str, Any]:
    user = "perf_burn"
    case_ids = ["CASE-P1", "CASE-P2", "CASE-P3"]
    batch_ids = [_new_batch_id() for _ in range(batches)]
    for bid in batch_ids:
        _seed_logs(ctx.data_dir, bid, case_ids)
    rtts: list[float] = []
    sessions = 0
    for bid in batch_ids:
        rtt, status, body = _deliver(ctx, _delivery_payload(bid, user, case_ids))
        if status != 202:
            raise RuntimeError(f"投递失败（{status}）：{body}")
        rtts.append(round(rtt, 3))
        sessions += len(body.get("sessions", []))
        print(f"  [burndown] {bid} 受理 {rtt * 1000:.1f}ms")
    completed = _wait_batches(ctx.db_url, batch_ids, timeout)
    events = _stage_events(ctx.app_lines)
    callbacks = _callback_metrics(ctx.mock_url, sessions)
    return {
        "batches": batches,
        "accept": _stats(rtts),
        "queue_to_clean_p95": (
            round(percentile(_durations(events, to_state="CLEANING"), 95), 3)
            if _durations(events, to_state="CLEANING")
            else None
        ),
        "ai_p95": (
            round(percentile(_durations(events, from_state="AI_RUNNING"), 95), 3)
            if _durations(events, from_state="AI_RUNNING")
            else None
        ),
        "queue_wait": bucket_waits(_queue_waits_from_events(events)),
        "fairness": {},
        "e2e": _e2e_buckets(ctx.db_url, batch_ids),
        "batch_completion_seconds": completed,
        "batch_p95": (round(percentile(list(completed.values()), 95), 3) if completed else None),
        "callbacks": callbacks,
    }


def run_fairness(ctx: Ctx, timeout: float) -> dict[str, Any]:
    users = ["perf_u_a", "perf_u_b", "perf_u_c"]
    case_ids = ["CASE-P1", "CASE-P2", "CASE-P3", "CASE-P4"]
    batch_ids: list[str] = []
    sessions = 0
    rtts: list[float] = []
    for user in users:
        bid = _new_batch_id()
        batch_ids.append(bid)
        _seed_logs(ctx.data_dir, bid, case_ids)
        rtt, status, body = _deliver(ctx, _delivery_payload(bid, user, case_ids))
        if status != 202:
            raise RuntimeError(f"公平性投递失败（{status}）：{body}")
        rtts.append(round(rtt, 3))
        sessions += len(body.get("sessions", []))
        print(f"  [fairness] {bid}（{user}）受理 {rtt * 1000:.1f}ms")
    completed = _wait_batches(ctx.db_url, batch_ids, timeout)
    events = _stage_events(ctx.app_lines)
    waits = _queue_waits_from_events(events)
    per_key_bucket: dict[tuple[str, str], list[float]] = {}
    for key, depth, seconds in waits:
        per_key_bucket.setdefault((key, depth_bucket(depth)), []).append(seconds)
    fairness = {
        bucket: fairness_gap(per_key_bucket, bucket=bucket) for bucket in ("0", "1-2", "3-4")
    }
    to_clean = _durations(events, to_state="CLEANING")
    return {
        "users": users,
        "tasks": (len(per_key_bucket) and sum(len(v) for v in per_key_bucket.values())) or 0,
        "accept": _stats(rtts),
        "queue_to_clean_p95": round(percentile(to_clean, 95), 3) if to_clean else None,
        "queue_wait": bucket_waits(waits),
        "fairness": fairness,
        "e2e": _e2e_buckets(ctx.db_url, batch_ids),
        "batch_completion_seconds": completed,
        "batch_p95": (round(percentile(list(completed.values()), 95), 3) if completed else None),
        "callbacks": _callback_metrics(ctx.mock_url, sessions),
    }


def run_backpressure(ctx: Ctx) -> dict[str, Any]:
    users = [f"perf_bp_{i}" for i in range(20)]
    accepted = 0
    rejected = 0
    retry_after: list[float] = []
    codes: dict[str, int] = {}
    results: list[tuple[int, dict[str, Any]]] = []
    with ThreadPoolExecutor(max_workers=10) as pool:
        futures = [
            pool.submit(_deliver, ctx, _delivery_payload(_new_batch_id(), user, ["CASE-P1"]))
            for user in users
        ]
        for future in futures:
            _, status, body = future.result()
            codes[str(status)] = codes.get(str(status), 0) + 1
            if status == 202:
                accepted += 1
            elif status == 429 and body.get("code") == "QUEUE_FULL":
                rejected += 1
                if isinstance(body.get("retry_after"), (int, float)):
                    retry_after.append(float(body["retry_after"]))
            results.append((status, body))
    samples = [body for status, body in results if status == 429][:2]
    return {
        "delivered": len(users),
        "accepted": accepted,
        "rejected_429": rejected,
        "retry_after_p95": round(percentile(retry_after, 95), 3) if retry_after else None,
        "status_codes": codes,
        "sample": samples,
    }


# ---------------------------------------------------------------- 组装与报告


def _report_rows(profile: str, metrics: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "metric": r.metric,
            "target": r.target,
            "measured": r.measured,
            "ok": r.ok,
        }
        for r in summarize(metrics, SloSettings())
    ]


def _print_summary(profile: str, metrics: dict[str, Any], rows: list[dict[str, Any]]) -> None:
    print(f"[{profile}]")
    if profile == "backpressure":
        print(
            f"  背压：投递 {metrics['delivered']}，受理 {metrics['accepted']}，"
            f"429 QUEUE_FULL {metrics['rejected_429']}，"
            f"retry_after P95 {metrics['retry_after_p95']}s"
        )
        return
    waits = metrics.get("queue_wait", {}) if isinstance(metrics.get("queue_wait"), dict) else {}
    print(
        f"  受理 P95 {metrics.get('accept', {}).get('p95')}s；"
        f"队列等待分桶 P95（秒） "
        f"{waits.get('0', {}).get('p95', '-')}/{waits.get('1-2', {}).get('p95', '-')}/"
        f"{waits.get('3-4', {}).get('p95', '-')}/{waits.get('>4', {}).get('p95', '-')}"
    )
    passed = sum(1 for row in rows if row["ok"])
    print(f"  SLO 对照：{passed}/{len(rows)} 达标")
    for row in rows:
        mark = "PASS" if row["ok"] else "MISS"
        print(f"    [{mark}] {row['metric']}：目标 {row['target']}，实测 {row['measured']}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="AITrace SLO 标定 harness")
    parser.add_argument("--batches", type=int, default=20, help="burndown 批次数（默认 20）")
    parser.add_argument("--ai-workers", type=int, default=2, help="AI Worker 池上限 W（默认 2）")
    parser.add_argument("--out", type=Path, default=Path("report/perf_report.json"))
    parser.add_argument(
        "--profile",
        choices=["fairness", "burndown", "backpressure", "all"],
        default="all",
    )
    parser.add_argument("--timeout", type=float, default=600.0, help="批次完成等待上限（秒）")
    args = parser.parse_args(argv)

    db_url = os.getenv("TEST_DATABASE_URL") or DEFAULT_DB_URL
    profiles = ["burndown", "fairness", "backpressure"] if args.profile == "all" else [args.profile]
    tmp = Path(tempfile.mkdtemp(prefix="aitrace-perf-"))
    data_dir = tmp / "data"
    _migrate(db_url)
    report: dict[str, Any] = {
        "generated_at": datetime.now(UTC).isoformat(),
        "params": {"batches": args.batches, "ai_workers": args.ai_workers, "db_url": db_url},
        "profiles": {},
        "slo": {},
    }
    try:
        for profile in profiles:
            _truncate_all(db_url)
            ctx = Ctx(
                tmp=tmp,
                db_url=db_url,
                ai_workers=args.ai_workers,
                app_lines=[],
                data_dir=data_dir,
            )
            extra_env: dict[str, str] | None = None
            if profile == "backpressure":
                extra_env = {
                    "AITRACE_QUEUE__GLOBAL_DEPTH": "2",
                    "AITRACE_QUEUE__PER_TOKEN_DEPTH": "2",
                }
            _start_services(ctx, extra_env)
            try:
                if profile == "burndown":
                    metrics = run_burndown(ctx, args.batches, args.timeout)
                elif profile == "fairness":
                    metrics = run_fairness(ctx, args.timeout)
                else:
                    metrics = run_backpressure(ctx)
            finally:
                _stop_services(ctx)
            dump_path = ctx.tmp / f"app_lines_{profile}.log"
            dump_path.write_text("".join(ctx.app_lines), encoding="utf-8")
            print(f"服务日志已保留：{dump_path}")
            report["profiles"][profile] = metrics
            if profile != "backpressure":
                rows = _report_rows(profile, metrics)
                report["slo"][profile] = rows
                _print_summary(profile, metrics, rows)
            else:
                _print_summary(profile, metrics, [])
    finally:
        pass
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"报告已写入：{args.out}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"标定失败：{exc}", file=sys.stderr)
        raise SystemExit(1) from exc
