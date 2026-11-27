from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest
from sqlalchemy import create_engine, text

from tests.conftest import requires_db

pytestmark = pytest.mark.e2e

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_TEST_DB_URL = "postgresql+psycopg://aitrace:aitrace@127.0.0.1:5432/aitrace_test"
TEST_DB_URL = os.getenv("TEST_DATABASE_URL") or DEFAULT_TEST_DB_URL
READ_TOKEN = "mock-read-token"
BATCH_1 = "BATCH-20261120-0089"  # 场景默认批次（正常全链）
BATCH_2 = "B-E2E-2"  # 令牌过期恢复链的覆盖批次

_TRUNCATE = """
TRUNCATE TABLE audit_log, attachment, callback_seq, profile, memory_hot_file,
case_contribution, case_library, user_queue, task_queue, batch, report, "round", "session"
RESTART IDENTITY CASCADE
"""

# 四路日志内容刻意避开截流闸规则（无 锁板冲突/盘被占用/Agent 失联/任务取消/Link Down），
# 保证清洗不短路、必然进入 AI 阶段。
_LOG_FILES = {
    "test.log": (
        "2026-11-20T10:00:02.000Z CMD nvme power-down @CMD uart_tick=12345\n"
        "2026-11-20T10:53:43.500Z ERROR TimeoutException: uart no response err_code=0x1A\n"
    ),
    "uart.log": "Build ID: X\n@TICK 12345\n",
    "dmesg.log": "2026-11-20T10:00:00.000Z kernel: nvme0n1: probe ok\n",
    "scheduler.log": "2026-11-20T10:00:00Z STATE RUNNING\n2026-11-20T10:55:00Z STATE EXIT\n",
}


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _stop(process: subprocess.Popen[bytes]) -> None:
    """terminate → wait → kill，保证子进程必然回收。"""
    if process.poll() is None:
        process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=10)


def _start_service(
    command: list[str], url: str, *, env: dict[str, str], ready_timeout: float = 15.0
) -> subprocess.Popen[bytes]:
    process = subprocess.Popen(
        command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env, cwd=ROOT
    )
    try:
        deadline = time.monotonic() + ready_timeout
        while time.monotonic() < deadline:
            if process.poll() is not None:
                output = process.stdout.read().decode(errors="replace") if process.stdout else ""
                raise RuntimeError(
                    f"服务提前退出（rc={process.returncode}）：{url}\n{output[-2000:]}"
                )
            try:
                if (
                    httpx.get(f"{url}/api/v1/health", timeout=0.5, trust_env=False).status_code
                    == 200
                ):
                    return process
            except httpx.HTTPError:
                pass
            time.sleep(0.1)
        raise RuntimeError(f"服务在 {ready_timeout}s 内未就绪：{url}")
    except BaseException:
        _stop(process)
        raise


def _seed_logs(data_dir: Path, batch_id: str) -> None:
    for name, content in _LOG_FILES.items():
        path = data_dir / "rawlogs" / batch_id / "var/log/aitest/CASE-1021" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


@pytest.fixture(scope="module", autouse=True)
def _prepare_e2e_db() -> None:
    if os.getenv("TEST_DATABASE_URL") is None:
        pytest.skip("TEST_DATABASE_URL is not set")
    subprocess.run(
        [sys.executable, "-m", "alembic", "-x", f"db_url={TEST_DB_URL}", "upgrade", "head"],
        check=True,
        cwd=ROOT,
    )
    engine = create_engine(TEST_DB_URL)
    with engine.begin() as conn:
        conn.execute(text(_TRUNCATE))
    engine.dispose()


@dataclass(frozen=True)
class Chain:
    mock_url: str
    aitrace_url: str
    data_dir: Path


@pytest.fixture(scope="module")
def chain(tmp_path_factory) -> Iterator[Chain]:
    """真实 uvicorn 子进程对：aitrace（fake AI + 调度循环开）↔ mock（回调接收）。"""
    tmp = tmp_path_factory.mktemp("e2e-ai")
    data_dir = tmp / "data"
    for batch_id in (BATCH_1, BATCH_2):
        _seed_logs(data_dir, batch_id)
    aitrace_port = _free_port()
    mock_port = _free_port()
    aitrace_proc = _start_service(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "aitrace.api.app:create_app",
            "--factory",
            "--port",
            str(aitrace_port),
            "--log-level",
            "warning",
        ],
        f"http://127.0.0.1:{aitrace_port}",
        env={
            **os.environ,
            "AITRACE_SERVICE_TOKEN": "dev-service-token",
            "AITRACE_DATABASE__DSN": TEST_DB_URL,
            "AITRACE_AI__EDITION": "fake",
            "AITRACE_AI__FAKE_SCENARIO_FILE": str(ROOT / "tests/fixtures/ai_fake_scenario.json"),
            "AITRACE_AI__PAUSE_BACKOFF_BASE_SECONDS": "1",
            "AITRACE_STORAGE__DATA_DIR": str(data_dir),
            "AITRACE_STORAGE__PROFILE_DOMAINS_DIR": str(ROOT / "deploy/config/domains"),
            "AITRACE_STORAGE__CUTOFF_RULES_FILE": str(ROOT / "deploy/config/rules/cutoff.yaml"),
            "AITRACE_AITEST__BASE_URL": f"http://127.0.0.1:{mock_port}",
            "AITRACE_AITEST__READ_TOKEN": READ_TOKEN,
        },
    )
    try:
        mock_proc = _start_service(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "aitest_mock.main:app",
                "--port",
                str(mock_port),
                "--log-level",
                "warning",
            ],
            f"http://127.0.0.1:{mock_port}",
            env={
                **os.environ,
                "AITRACE_BASE_URL": f"http://127.0.0.1:{aitrace_port}",
                "AITRACE_SERVICE_TOKEN": "dev-service-token",
                "AITEST_READ_TOKEN": READ_TOKEN,
            },
            ready_timeout=5.0,
        )
    except BaseException:
        _stop(aitrace_proc)
        raise
    try:
        yield Chain(
            mock_url=f"http://127.0.0.1:{mock_port}",
            aitrace_url=f"http://127.0.0.1:{aitrace_port}",
            data_dir=data_dir,
        )
    finally:
        _stop(mock_proc)
        _stop(aitrace_proc)


def _wait_batch_state(batch_id: str, state: str, timeout: float = 180.0) -> None:
    deadline = time.monotonic() + timeout
    engine = create_engine(TEST_DB_URL)
    try:
        while time.monotonic() < deadline:
            with engine.connect() as conn:
                current = conn.execute(
                    text("SELECT batch_state FROM batch WHERE batch_id = :bid"),
                    {"bid": batch_id},
                ).scalar()
            if current == state:
                return
            time.sleep(0.5)
        raise AssertionError(f"等待批次 {batch_id} 状态 {state} 超时（当前 {current}）")
    finally:
        engine.dispose()


def _callbacks(mock_url: str) -> list[dict]:
    return httpx.get(
        f"{mock_url}/__mock/callbacks",
        headers={"X-Aitest-Read-Token": READ_TOKEN},
        timeout=5.0,
        trust_env=False,
    ).json()["items"]


def _wait_callback(
    mock_url: str,
    *,
    event: str,
    session_id: str | None = None,
    start: int = 0,
    timeout: float = 120.0,
) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for item in _callbacks(mock_url)[start:]:
            if item["event"] == event and (
                session_id is None or item.get("session_id") == session_id
            ):
                return item
        time.sleep(0.5)
    raise AssertionError(f"等待回调超时：event={event} session={session_id}")


@requires_db
def test_full_chain_deliver_to_callback(chain: Chain) -> None:
    """deliver → 清洗 → 路由 → fake AI → 报告回写 → RESULT 回调 → 批次关账 COMPLETED。"""
    headers = {"X-Aitest-Read-Token": READ_TOKEN}
    with httpx.Client(timeout=15.0, trust_env=False) as client:
        delivered = client.post(f"{chain.mock_url}/api/v1/aitest/deliver", json={}, headers=headers)
        assert delivered.status_code == 202, delivered.text
        session_id: str = delivered.json()["sessions"][0]["session_id"]
        assert session_id
    _wait_batch_state(BATCH_1, "COMPLETED", timeout=180.0)
    result = _wait_callback(chain.mock_url, event="RESULT", session_id=session_id)
    report = result["report"]
    assert report["root_cause"]["category"] == "FW_DEFECT"
    assert isinstance(report["confidence"], float) and 0 <= report["confidence"] <= 1
    progress = _wait_callback(chain.mock_url, event="PROGRESS", session_id=session_id)
    assert progress["stage"] in {"AI_CLASSIFYING", "DEEP_PROBING"}


@requires_db
def test_token_expired_pauses_then_recovers(chain: Chain) -> None:
    """令牌过期 → PAUSED_AUTH 回调 → 恢复令牌 → 重试成功 → RESULT + 批次 COMPLETED。"""
    headers = {"X-Aitest-Read-Token": READ_TOKEN}
    with httpx.Client(timeout=15.0, trust_env=False) as client:
        start = len(_callbacks(chain.mock_url))
        switched = client.post(
            f"{chain.mock_url}/__mock/scenario",
            json={"token_scenario": "token_expired"},
            headers=headers,
        )
        assert switched.status_code == 200
        delivered = client.post(
            f"{chain.mock_url}/api/v1/aitest/deliver",
            json={"batch_id": BATCH_2},
            headers=headers,
        )
        assert delivered.status_code == 202, delivered.text
        session_id: str = delivered.json()["sessions"][0]["session_id"]
        paused = _wait_callback(
            chain.mock_url, event="QUEUE_STATE", session_id=None, start=start, timeout=60.0
        )
        assert paused["queue_event"]["type"] == "PAUSED_AUTH"
        switched = client.post(
            f"{chain.mock_url}/__mock/scenario",
            json={"token_scenario": None},
            headers=headers,
        )
        assert switched.status_code == 200
    _wait_batch_state(BATCH_2, "COMPLETED", timeout=300.0)
    result = _wait_callback(chain.mock_url, event="RESULT", session_id=session_id, start=start)
    assert result["report"]["root_cause"]["category"] == "FW_DEFECT"
