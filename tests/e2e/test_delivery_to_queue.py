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
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from aitrace.api.app import create_app
from aitrace.core.settings import Settings
from tests.conftest import requires_db
from tests.integration.test_delivery_api import HEADERS, _payload

pytestmark = pytest.mark.e2e

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_TEST_DB_URL = "postgresql+psycopg://aitrace:aitrace@127.0.0.1:5432/aitrace_test"
TEST_DB_URL = os.getenv("TEST_DATABASE_URL") or DEFAULT_TEST_DB_URL

_TRUNCATE = """
TRUNCATE TABLE audit_log, attachment, profile, memory_hot_file, case_contribution,
case_library, user_queue, task_queue, batch, report, "round", "session"
RESTART IDENTITY CASCADE
"""


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
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=env,
        cwd=ROOT,
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
        # 任何启动失败都必须回收子进程，避免端口/进程泄漏。
        _stop(process)
        raise


@pytest.fixture(scope="module", autouse=True)
def _prepare_e2e_db() -> None:
    """迁移到最新并清空测试库，保证交付类用例重复执行仍拿到 202（幂等）。"""
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


@pytest.fixture(scope="module")
def mock_url() -> str:
    port = _free_port()
    process = _start_service(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "aitest_mock.main:app",
            "--port",
            str(port),
            "--log-level",
            "warning",
        ],
        f"http://127.0.0.1:{port}",
        env=dict(os.environ),
        ready_timeout=5.0,
    )
    yield f"http://127.0.0.1:{port}"
    _stop(process)


def test_delivery_to_queue_smoke(mock_url: str, test_settings: Settings) -> None:
    token = httpx.get(
        f"{mock_url}/api/v1/aitest/token",
        params={
            "product_id": "ssd_9560",
            "batch_id": "BATCH-20261120-0089",
            "case_id": "CASE-1021",
            "trigger_user_id": "zhangsan",
            "action": "auto",
        },
        headers={"X-Aitest-Read-Token": "mock-read-token"},
        trust_env=False,
    )
    assert token.status_code == 200

    with TestClient(create_app(test_settings)) as client:
        accepted = client.post("/api/v1/aitrace/tasks", json=_payload(), headers=HEADERS)
        assert accepted.status_code == 202
        session_id = accepted.json()["sessions"][0]["session_id"]
        status = client.get(f"/api/v1/aitrace/tasks/{session_id}", headers=HEADERS)
    assert status.status_code == 200
    assert status.json()["stage"] == "QUEUED"
    assert status.json()["progress"] == 10


@dataclass(frozen=True)
class Chain:
    """两条真实 uvicorn 子进程的地址：mock 面向测试，aitrace 面向 mock。"""

    mock_url: str
    aitrace_url: str


@pytest.fixture(scope="module")
def full_chain() -> Iterator[Chain]:
    # 1. 迁移（幂等）并清库，确保本轮投递是全新 202。
    subprocess.run(
        [sys.executable, "-m", "alembic", "-x", f"db_url={TEST_DB_URL}", "upgrade", "head"],
        check=True,
        cwd=ROOT,
    )
    engine = create_engine(TEST_DB_URL)
    with engine.begin() as conn:
        conn.execute(text(_TRUNCATE))
    engine.dispose()

    # 2. 启动知因：create_app 是无参工厂，走 uvicorn --factory（与 Makefile dev 一致）。
    aitrace_port = _free_port()
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
            "AITRACE_RUNTIME__ENABLED": "false",  # M1 e2e 只断言 QUEUED，禁止调度循环抢跑
        },
    )
    mock_port = _free_port()
    try:
        # 3. 启动 aitest mock，指向真实知因。
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
                "AITEST_READ_TOKEN": "mock-read-token",
            },
        )
    except BaseException:
        _stop(aitrace_proc)
        raise
    try:
        yield Chain(
            mock_url=f"http://127.0.0.1:{mock_port}",
            aitrace_url=f"http://127.0.0.1:{aitrace_port}",
        )
    finally:
        _stop(mock_proc)
        _stop(aitrace_proc)


@requires_db
def test_full_chain_mock_delivers_to_real_aitrace(full_chain: Chain) -> None:
    """全链路：mock → 真实 AITrace HTTP → 状态读回，全部走网络而非 TestClient。"""
    headers = {"X-Aitest-Read-Token": "mock-read-token"}
    with httpx.Client(timeout=15.0, trust_env=False) as client:
        token = client.get(
            f"{full_chain.mock_url}/api/v1/aitest/token",
            params={
                "product_id": "ssd_9560",
                "batch_id": "BATCH-20261120-0089",
                "case_id": "CASE-1021",
                "trigger_user_id": "zhangsan",
                "action": "auto",
            },
            headers=headers,
        )
        assert token.status_code == 200
        assert token.json()["token"].startswith("mock-token-")

        delivered = client.post(
            f"{full_chain.mock_url}/api/v1/aitest/deliver", json={}, headers=headers
        )
        assert delivered.status_code in (200, 202), delivered.text
        body = delivered.json()
        assert body["batch_task_id"] == "T-BATCH-20261120-0089"
        assert body["sessions"], body
        session_id: str = body["sessions"][0]["session_id"]

        status = client.get(
            f"{full_chain.mock_url}/api/v1/aitest/aitrace/status/{session_id}",
            headers=headers,
        )
        assert status.status_code == 200
        view = status.json()
        assert view["session_id"] == session_id
        assert view["stage"] == "QUEUED"
        assert view["progress"] == 10
        assert view["round_no"] == 1

        callbacks = client.get(f"{full_chain.mock_url}/__mock/callbacks", headers=headers)
        assert callbacks.status_code == 200
        assert isinstance(callbacks.json()["items"], list)
