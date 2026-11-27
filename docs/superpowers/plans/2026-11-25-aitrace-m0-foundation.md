# AITrace M0 Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stand up the AITrace service skeleton with frozen inbound contracts, fail-fast configuration, domain profiles, the full V1 PostgreSQL schema, an append-only audit hash chain, a DB task queue with lease/fencing, a token-queue slot gate, delivery ingestion, session status query, an isolated aitest mock, and the Linux deployment skeleton.

**Architecture:** A single ASGI process built by `create_app(settings)` with thin FastAPI routes over service and repository layers. All durable state lives in PostgreSQL; all queue claims use `FOR UPDATE SKIP LOCKED` plus lease generation fencing. Profiles are loaded from YAML at startup into immutable resolved objects. Tests are split into pure unit tests, real-PostgreSQL integration tests, contract drift tests, and one end-to-end smoke.

**Tech Stack:** Python 3.13, FastAPI, Pydantic v2 + pydantic-settings, SQLAlchemy 2.0 sync + psycopg 3, PostgreSQL 16, Alembic, PyYAML, pytest + httpx, Ruff, mypy, GitHub Actions.

**Spec:** [docs/superpowers/specs/2026-11-25-aitrace-design-v1-final.md](../specs/2026-11-25-aitrace-design-v1-final.md)

## Global Constraints

- `qoder_access_token` never appears in requests, responses, the database, queue rows, audit rows, config files, env files, logs, or test fixtures. The inbound schema forbids unknown fields, so a payload carrying it is rejected with `INVALID_PAYLOAD`.
- No platform fallback token exists. M0 must not add any token storage path.
- All API error bodies use `{ code, message, retryable, retry_after?, details? }`. Validation errors are HTTP 400 `INVALID_PAYLOAD` with Chinese field labels, the rejected value, and allowed values.
- All timestamps are timezone-aware UTC. New queue rows use `lease_until = epoch`, never `NULL`.
- Migrations are the only way to create schema; application code never calls `create_all` outside tests.
- Only `src/aitrace` is production code. `mocks/aitest` must not import `aitrace`; tests must not contain production logic.
- Every functional behavior gets a test that fails when the behavior is removed.
- Commit steps in this plan are checkpoints. Execute them only after the user approves the phase; never push.

---

## File Map

| Path | Responsibility |
|---|---|
| `pyproject.toml` | Package, dependencies, tool configuration |
| `Makefile` | Developer and CI entry points |
| `.github/workflows/ci.yml` | Lint, unit, integration, contract gates |
| `src/aitrace/__init__.py` | Package version |
| `src/aitrace/main.py` | `aitrace-server` entry point |
| `src/aitrace/core/enums.py` | Task, queue, stage, phase, waiting-reason enums |
| `src/aitrace/core/errors.py` | `AitraceError` business exception |
| `src/aitrace/core/ids.py` | `Clock`, `IdGenerator` implementations |
| `src/aitrace/core/settings.py` | Layered settings, fail-fast validation, Chinese messages |
| `src/aitrace/core/audit.py` | Append-only SHA-256 hash chain |
| `src/aitrace/api/app.py` | Application factory |
| `src/aitrace/api/deps.py` | Settings, session, resolver, auth dependencies |
| `src/aitrace/api/errors.py` | Unified error handlers |
| `src/aitrace/api/schemas.py` | Delivery, response, status DTOs |
| `src/aitrace/api/routes/health.py` | Liveness |
| `src/aitrace/api/routes/deliveries.py` | `POST /api/v1/aitrace/tasks` |
| `src/aitrace/api/routes/sessions.py` | `GET /api/v1/aitrace/tasks/{session_id}` |
| `src/aitrace/persistence/base.py` | Declarative base, epoch constant |
| `src/aitrace/persistence/models.py` | Twelve core tables |
| `src/aitrace/persistence/repositories/queue.py` | Enqueue, claim, finish, retry, reclaim |
| `src/aitrace/persistence/repositories/profiles.py` | Profile snapshot upsert |
| `src/aitrace/persistence/migrations/` | Alembic environment and initial revision |
| `src/aitrace/ingest/service.py` | Idempotent delivery acceptance |
| `src/aitrace/profiles/models.py` | Domain profile schema |
| `src/aitrace/profiles/loader.py` | Registry and profile file loading |
| `src/aitrace/profiles/resolver.py` | Product → domain resolution and versioning |
| `src/aitrace/scheduling/types.py` | `NewTask` |
| `src/aitrace/scheduling/slot_gate.py` | `user_queue` slot acquire/release |
| `src/aitrace/scheduling/stage_view.py` | `task_queue` → public stage mapping |
| `contracts/` | Generated OpenAPI and JSON Schemas |
| `tools/export_contracts.py` | Contract generation and drift check |
| `mocks/aitest/` | Independent aitest mock process |
| `deploy/` | Config templates, domains, systemd, logrotate, scripts |
| `data/README.md`, `deploy/secrets/README.md` | Runtime-data and secret conventions |
| `tests/unit`, `tests/integration`, `tests/contract`, `tests/e2e`, `tests/ops` | Layered tests |

---

### Task 1: Bootstrap the project and test tooling

**Files:**
- Create: `pyproject.toml`
- Create: `Makefile`
- Create: `src/aitrace/__init__.py`
- Create: `tests/__init__.py`, `tests/unit/__init__.py`, `tests/integration/__init__.py`, `tests/contract/__init__.py`, `tests/e2e/__init__.py`, `tests/ops/__init__.py`
- Create: `tests/unit/test_package.py`
- Create: `.github/workflows/ci.yml`
- Create: `data/README.md`, `deploy/secrets/README.md`, `mocks/aitest/README.md`
- Modify: `.gitignore` (only if a listed ignore is missing)

**Interfaces:**
- Produces: importable package `aitrace` with `__version__ = "0.1.0"`; `make lint`, `make test`, `make test-unit`, `make test-integration`, `make contract`, `make e2e`, `make migrate`, `make run`, `make mock`, `make install-mock`.

- [ ] **Step 1: Write the failing package test**

```python
# tests/unit/test_package.py
import aitrace


def test_package_exposes_version() -> None:
    assert aitrace.__version__ == "0.1.0"
```

- [ ] **Step 2: Run it and confirm the package does not exist**

Run: `pytest tests/unit/test_package.py -v`

Expected: FAIL with `ModuleNotFoundError: No module named 'aitrace'`.

- [ ] **Step 3: Create packaging, Makefile, and CI**

```toml
# pyproject.toml
[build-system]
requires = ["hatchling>=1.25"]
build-backend = "hatchling.build"

[project]
name = "aitrace"
version = "0.1.0"
description = "AITrace failure analysis sidecar"
requires-python = ">=3.13"
dependencies = [
  "fastapi>=0.115,<1",
  "uvicorn[standard]>=0.32,<1",
  "pydantic>=2.9,<3",
  "pydantic-settings>=2.5,<3",
  "sqlalchemy>=2.0.35,<2.1",
  "psycopg[binary]>=3.2,<4",
  "alembic>=1.13,<2",
  "pyyaml>=6.0,<7",
]

[project.optional-dependencies]
dev = [
  "pytest>=8.3,<9",
  "pytest-cov>=5,<7",
  "httpx>=0.27,<1",
  "ruff>=0.7,<1",
  "mypy>=1.11,<2",
  "types-PyYAML>=6.0",
]

[project.scripts]
aitrace-server = "aitrace.main:run"

[tool.hatch.build.targets.wheel]
packages = ["src/aitrace"]

[tool.pytest.ini_options]
testpaths = ["tests"]
addopts = "-ra --strict-markers"
markers = [
  "integration: requires TEST_DATABASE_URL",
  "e2e: full local chain",
]

[tool.ruff]
line-length = 100
target-version = "py313"
src = ["src", "tests", "mocks/aitest/src"]

[tool.ruff.lint]
select = ["E", "F", "I", "UP", "B", "SIM", "RUF"]

[tool.mypy]
python_version = "3.13"
files = ["src/aitrace"]
disallow_untyped_defs = true
no_implicit_optional = true
warn_redundant_casts = true
warn_unused_ignores = true
plugins = ["pydantic.mypy"]

[[tool.mypy.overrides]]
module = ["yaml.*"]
ignore_missing_imports = true
```

```python
# src/aitrace/__init__.py
__version__ = "0.1.0"
```

Test modules import shared fixtures and payload builders by package path, so create the test packages now:

```bash
mkdir -p tests/unit tests/integration tests/contract tests/e2e tests/ops
touch tests/__init__.py tests/unit/__init__.py tests/integration/__init__.py \
      tests/contract/__init__.py tests/e2e/__init__.py tests/ops/__init__.py
```

```make
# Makefile
PY ?= python3.13
VENV ?= .venv
BIN := $(VENV)/bin
TEST_DATABASE_URL ?= postgresql+psycopg://aitrace:aitrace@127.0.0.1:5432/aitrace_test

.PHONY: install install-mock dev mock test test-unit test-integration contract e2e lint migrate run verify

install:
	$(PY) -m venv $(VENV)
	$(BIN)/pip install -e ".[dev]"

install-mock:
	$(BIN)/pip install -e mocks/aitest

dev:
	$(BIN)/uvicorn aitrace.api.app:create_app --factory --reload --host 127.0.0.1 --port 8080

mock:
	$(BIN)/uvicorn aitest_mock.main:app --host 127.0.0.1 --port 8090

test:
	TEST_DATABASE_URL=$(TEST_DATABASE_URL) $(BIN)/pytest --cov=aitrace --cov-fail-under=85

test-unit:
	$(BIN)/pytest tests/unit

test-integration:
	TEST_DATABASE_URL=$(TEST_DATABASE_URL) $(BIN)/pytest tests/integration

contract:
	$(BIN)/python tools/export_contracts.py --check
	$(BIN)/pytest tests/contract

e2e:
	TEST_DATABASE_URL=$(TEST_DATABASE_URL) $(BIN)/pytest tests/e2e

lint:
	$(BIN)/ruff check .
	$(BIN)/ruff format --check .
	$(BIN)/mypy

migrate:
	AITRACE_DATABASE__DSN=$(TEST_DATABASE_URL) $(BIN)/alembic upgrade head

run:
	$(BIN)/aitrace-server

verify: lint test contract e2e
```

```yaml
# .github/workflows/ci.yml
name: ci
on:
  push:
    branches: [main]
  pull_request:

jobs:
  verify:
    runs-on: ubuntu-latest
    services:
      postgres:
        image: postgres:16
        env:
          POSTGRES_USER: aitrace
          POSTGRES_PASSWORD: aitrace
          POSTGRES_DB: aitrace_test
        ports: ["5432:5432"]
        options: >-
          --health-cmd "pg_isready -U aitrace"
          --health-interval 5s
          --health-timeout 5s
          --health-retries 20
    env:
      TEST_DATABASE_URL: postgresql+psycopg://aitrace:aitrace@127.0.0.1:5432/aitrace_test
      AITRACE_SERVICE_TOKEN: ci-service-token
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.13"
      - run: pip install -e ".[dev]" -e mocks/aitest
      - run: make lint
      - run: make test
      - run: make contract
      - run: make e2e
```

`data/README.md`:

```markdown
# 运行数据根

生产路径为 `/var/lib/aitrace`，开发默认 `data/`。子目录 `rawlogs/`、`evidence/`、`runtime/`、`memory/`、`logs/`、`backups/` 均由服务创建；除本文件外不入版本库。
```

`deploy/secrets/README.md`:

```markdown
# 密钥约定

本目录只保存 KMS / Secret 引用说明。生产环境通过 systemd `LoadCredential=` 注入，文件位于 `/etc/aitrace/credentials/`，不得提交任何真实密钥。`qoder_access_token` 不属于本目录，任何情况下不得写入磁盘。
```

`mocks/aitest/README.md`:

```markdown
# aitest mock

独立 FastAPI 进程，模拟 aitest 的 JIT 令牌、用例清单与回调接收端点。禁止依赖 `src/aitrace`。启动：`make mock`。
```

- [ ] **Step 4: Install and run the test**

Run: `python3.13 -m venv .venv && .venv/bin/pip install -e ".[dev]" && .venv/bin/pytest tests/unit/test_package.py -v`

Expected: PASS.

- [ ] **Step 5: Commit checkpoint**

```bash
git add pyproject.toml Makefile src/aitrace/__init__.py tests/unit/test_package.py .github/workflows/ci.yml data/README.md deploy/secrets/README.md mocks/aitest/README.md .gitignore
git commit -m "chore: bootstrap aitrace package and test tooling"
```

---

### Task 2: Fail-fast settings with Chinese diagnostics

**Files:**
- Create: `src/aitrace/core/__init__.py`
- Create: `src/aitrace/core/settings.py`
- Test: `tests/unit/test_settings.py`

**Interfaces:**
- Produces: `Settings`, `SettingsError`, `load_settings(config_path: Path | None = None) -> Settings`; fields `server.host/port/log_level`, `database.dsn/pool_size/statement_timeout_ms`, `queue.cleaning_concurrency/ai_worker_concurrency/per_token_depth/global_depth/lease_seconds/ai_hard_timeout_seconds/aging_interval_seconds`, `storage.data_dir/max_attachment_bytes/profile_domains_dir`, `service_token`, `service_token_file`, `env`.

- [ ] **Step 1: Write the failing settings tests**

```python
# tests/unit/test_settings.py
from pathlib import Path

import pytest

from aitrace.core.settings import SettingsError, load_settings


def _write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_yaml_and_env_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AITRACE_SERVICE_TOKEN", "s3cret")
    monkeypatch.setenv("AITRACE_QUEUE__LEASE_SECONDS", "900")
    settings = load_settings(_write(tmp_path, "env: dev\nserver:\n  port: 9090\n"))
    assert settings.server.port == 9090
    assert settings.queue.lease_seconds == 900
    assert settings.service_token is not None
    assert settings.service_token.get_secret_value() == "s3cret"


def test_token_file_is_used_when_env_is_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("AITRACE_SERVICE_TOKEN", raising=False)
    token_file = tmp_path / "service-token"
    token_file.write_text("file-token\n", encoding="utf-8")
    monkeypatch.setenv("AITRACE_SERVICE_TOKEN_FILE", str(token_file))
    settings = load_settings(None)
    assert settings.service_token is not None
    assert settings.service_token.get_secret_value() == "file-token"


def test_missing_token_names_the_field_in_chinese(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("AITRACE_SERVICE_TOKEN", raising=False)
    monkeypatch.delenv("AITRACE_SERVICE_TOKEN_FILE", raising=False)
    with pytest.raises(SettingsError) as caught:
        load_settings(_write(tmp_path, "env: dev\n"))
    message = str(caught.value)
    assert "配置校验失败" in message
    assert "服务令牌" in message


def test_unknown_key_is_rejected_with_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AITRACE_SERVICE_TOKEN", "s3cret")
    with pytest.raises(SettingsError) as caught:
        load_settings(_write(tmp_path, "queue:\n  lease_second: 900\n"))
    assert "lease_second" in str(caught.value)


def test_out_of_range_value_reports_current_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AITRACE_SERVICE_TOKEN", "s3cret")
    with pytest.raises(SettingsError) as caught:
        load_settings(_write(tmp_path, "server:\n  port: 70000\n"))
    message = str(caught.value)
    assert "服务端口" in message
    assert "70000" in message
```

- [ ] **Step 2: Run tests and confirm failure**

Run: `pytest tests/unit/test_settings.py -v`

Expected: FAIL with `ModuleNotFoundError: No module named 'aitrace.core'`.

- [ ] **Step 3: Implement settings**

```python
# src/aitrace/core/settings.py
from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict


class SettingsError(RuntimeError):
    """Raised when configuration is missing or invalid; the service must not start."""


_FIELD_LABELS = {
    "env": "运行环境",
    "service_token": "服务令牌",
    "service_token_file": "服务令牌文件",
    "server.host": "监听地址",
    "server.port": "服务端口",
    "server.log_level": "日志级别",
    "database.dsn": "数据库连接串",
    "database.pool_size": "数据库连接池大小",
    "database.statement_timeout_ms": "数据库语句超时",
    "queue.cleaning_concurrency": "清洗池并发",
    "queue.ai_worker_concurrency": "AI Worker 并发",
    "queue.per_token_depth": "单令牌队列深度",
    "queue.global_depth": "全局队列深度",
    "queue.lease_seconds": "任务租约时长",
    "queue.ai_hard_timeout_seconds": "AI 硬超时",
    "queue.aging_interval_seconds": "优先级老化间隔",
    "storage.data_dir": "数据根目录",
    "storage.max_attachment_bytes": "附件大小上限",
    "storage.profile_domains_dir": "领域画像目录",
}


class ServerSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    host: str = "127.0.0.1"
    port: int = Field(default=8080, ge=1, le=65535)
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"


class DatabaseSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    dsn: str = "postgresql+psycopg://aitrace:aitrace@127.0.0.1:5432/aitrace"
    pool_size: int = Field(default=10, ge=1, le=100)
    statement_timeout_ms: int = Field(default=30_000, ge=1_000)


class QueueSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    cleaning_concurrency: int = Field(default=4, ge=1, le=64)
    ai_worker_concurrency: int = Field(default=4, ge=1, le=32)
    per_token_depth: int = Field(default=100, ge=1)
    global_depth: int = Field(default=500, ge=1)
    lease_seconds: int = Field(default=600, ge=30)
    ai_hard_timeout_seconds: int = Field(default=1800, ge=60)
    aging_interval_seconds: int = Field(default=300, ge=30)


class StorageSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    data_dir: Path = Path("data")
    max_attachment_bytes: int = Field(default=50 * 1024 * 1024, ge=1024 * 1024)
    profile_domains_dir: Path = Path("deploy/config/domains")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="AITRACE_",
        env_nested_delimiter="__",
        extra="forbid",
        env_file=None,
    )

    env: Literal["dev", "staging", "prod"] = "dev"
    server: ServerSettings = Field(default_factory=ServerSettings)
    database: DatabaseSettings = Field(default_factory=DatabaseSettings)
    queue: QueueSettings = Field(default_factory=QueueSettings)
    storage: StorageSettings = Field(default_factory=StorageSettings)
    service_token: SecretStr | None = None
    service_token_file: Path | None = None


def load_settings(config_path: Path | None = None) -> Settings:
    values: dict[str, object] = {}
    if config_path is not None:
        if not config_path.is_file():
            raise SettingsError(f"配置文件不存在：{config_path}")
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        if raw is not None and not isinstance(raw, dict):
            raise SettingsError(f"配置文件顶层必须是映射：{config_path}")
        values = raw or {}
    try:
        settings = Settings(**values)
    except ValidationError as exc:
        raise SettingsError(_format_validation_error(exc)) from exc

    if settings.service_token is None and settings.service_token_file is not None:
        token = settings.service_token_file.read_text(encoding="utf-8").strip()
        if not token:
            raise SettingsError("配置校验失败，服务拒绝启动：\n- 服务令牌文件为空（service_token_file）")
        settings = settings.model_copy(update={"service_token": SecretStr(token)})
    if settings.service_token is None:
        raise SettingsError(
            "配置校验失败，服务拒绝启动：\n"
            "- 服务令牌（service_token）：未提供；要求：设置 AITRACE_SERVICE_TOKEN，"
            "或通过 AITRACE_SERVICE_TOKEN_FILE 指向 LoadCredential 文件"
        )
    return settings


def _format_validation_error(exc: ValidationError) -> str:
    lines = ["配置校验失败，服务拒绝启动："]
    for error in exc.errors():
        path = ".".join(str(part) for part in error["loc"])
        label = _FIELD_LABELS.get(path, path)
        supplied = error.get("input")
        current = "<缺失>" if supplied is None else repr(supplied)
        lines.append(f"- {label}（{path}）：当前值 {current}；要求：{error['msg']}")
    return "\n".join(lines)
```

- [ ] **Step 4: Run tests and fix until green**

Run: `pytest tests/unit/test_settings.py -v`

Expected: 5 passed.

- [ ] **Step 5: Commit checkpoint**

```bash
git add src/aitrace/core tests/unit/test_settings.py
git commit -m "feat: add fail-fast layered settings"
```

---

### Task 3: Application factory, health endpoint, and unified errors

**Files:**
- Create: `src/aitrace/core/enums.py`
- Create: `src/aitrace/core/errors.py`
- Create: `src/aitrace/core/ids.py`
- Create: `src/aitrace/api/__init__.py`
- Create: `src/aitrace/api/errors.py`
- Create: `src/aitrace/api/app.py`
- Create: `src/aitrace/api/deps.py`
- Create: `src/aitrace/api/routes/__init__.py`
- Create: `src/aitrace/api/routes/health.py`
- Create: `src/aitrace/main.py`
- Test: `tests/unit/test_errors.py`
- Test: `tests/unit/test_health.py`
- Test: `tests/conftest.py`

**Interfaces:**
- Produces: `create_app(settings: Settings | None = None) -> FastAPI`; `GET /api/v1/health` returning `{"status":"ok","version":"0.1.0","env":...}`; `AitraceError(code, message, http_status, retryable, retry_after)`; `SystemClock.now() -> datetime`; `UuidIdGenerator.new(prefix) -> str`; `describe_validation(errors) -> tuple[str, list[dict]]`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_errors.py
from fastapi.exceptions import RequestValidationError

from aitrace.api.errors import describe_validation


def test_validation_message_uses_chinese_label_and_allowed_values() -> None:
    exc = RequestValidationError(
        [
            {
                "type": "literal_error",
                "loc": ("body", "hardware_type"),
                "msg": "Input should be 'SSD' or 'CXL'",
                "input": "HDD",
                "ctx": {"expected": "'SSD' or 'CXL'"},
            }
        ]
    )
    message, details = describe_validation(exc.errors())
    assert "硬件类型" in message
    assert "HDD" in message
    assert "合法取值" in message
    assert details[0]["field"] == "hardware_type"
```

```python
# tests/unit/test_health.py
from fastapi.testclient import TestClient
from pydantic import SecretStr

from aitrace.api.app import create_app
from aitrace.core.settings import Settings


def test_health_returns_ok() -> None:
    app = create_app(Settings(service_token=SecretStr("test-token")))
    with TestClient(app) as client:
        response = client.get("/api/v1/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "version": "0.1.0", "env": "dev"}
```

```python
# tests/conftest.py
import os

import pytest
from pydantic import SecretStr

from aitrace.core.settings import DatabaseSettings, Settings

TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL")


@pytest.fixture
def test_settings() -> Settings:
    return Settings(
        service_token=SecretStr("test-service-token"),
        database=DatabaseSettings(dsn=TEST_DATABASE_URL or "postgresql+psycopg://aitrace:aitrace@127.0.0.1:5432/aitrace_test"),
    )
```

- [ ] **Step 2: Run and confirm failure**

Run: `pytest tests/unit/test_errors.py tests/unit/test_health.py -v`

Expected: FAIL with `ModuleNotFoundError: No module named 'aitrace.api'`.

- [ ] **Step 3: Implement enums, errors, IDs, app factory, health**

```python
# src/aitrace/core/enums.py
from enum import StrEnum


class TaskType(StrEnum):
    CASE_CLEAN = "case_clean"
    CASE_AI = "case_ai"
    BATCH_COMMON = "batch_common"
    NOTIFY = "notify"
    CALLBACK = "callback"
    MEMORY_MATERIALIZE = "memory_materialize"


class QueueState(StrEnum):
    PENDING = "PENDING"
    CLEANING = "CLEANING"
    WAITING = "WAITING"
    READY = "READY"
    AI_RUNNING = "AI_RUNNING"
    DONE = "DONE"
    FAILED = "FAILED"


class DispatchPhase(StrEnum):
    JIT_FETCH = "JIT_FETCH"
    AI_EXEC = "AI_EXEC"


class Stage(StrEnum):
    QUEUED = "QUEUED"
    CLEANING = "CLEANING"
    AI_CLASSIFYING = "AI_CLASSIFYING"
    DEEP_PROBING = "DEEP_PROBING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class WaitingReason(StrEnum):
    TOKEN_BUSY = "token_busy"
    CITATION_PENDING = "citation_pending"
    NOT_BEFORE = "not_before"
    RETRY_BACKOFF = "retry_backoff"
    PAUSED_AUTH = "paused_auth"


class UserQueueState(StrEnum):
    ACTIVE = "ACTIVE"
    PAUSED_AUTH = "PAUSED_AUTH"


class BatchState(StrEnum):
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
```

```python
# src/aitrace/core/errors.py
from __future__ import annotations


class AitraceError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        http_status: int = 400,
        retryable: bool = False,
        retry_after: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status
        self.retryable = retryable
        self.retry_after = retry_after
```

```python
# src/aitrace/core/ids.py
from __future__ import annotations

import secrets
from datetime import UTC, datetime


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class UuidIdGenerator:
    def new(self, prefix: str) -> str:
        stamp = datetime.now(UTC).strftime("%Y%m%d")
        return f"{prefix}-{stamp}-{secrets.token_hex(4).upper()}"
```

```python
# src/aitrace/api/errors.py
from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from aitrace.core.errors import AitraceError

_FIELD_LABELS = {
    "product_id": "产品 ID",
    "batch_id": "批次 ID",
    "trigger_user_id": "触发人",
    "hardware_type": "硬件类型",
    "env_topology": "环境拓扑",
    "git_context": "代码上下文",
    "failed_cases": "失败用例列表",
    "passed_cases": "通过用例列表",
    "case_id": "用例 ID",
    "script_path": "脚本路径",
    "failed_at_utc": "失败时间",
    "env_binding": "环境绑定",
    "logs": "日志路径",
    "devices": "设备清单",
    "host_ip": "主机 IP",
}


def describe_validation(errors: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
    details: list[dict[str, Any]] = []
    parts: list[str] = []
    for error in errors:
        loc = [str(part) for part in error.get("loc", ()) if part != "body"]
        field = ".".join(loc) if loc else "payload"
        leaf = loc[-1] if loc else "payload"
        label = _FIELD_LABELS.get(leaf, field)
        allowed = error.get("ctx", {}).get("expected")
        details.append(
            {
                "field": field,
                "label": label,
                "input": error.get("input"),
                "message": error.get("msg", ""),
                "allowed": allowed,
            }
        )
        text = f"{label} 当前值 {error.get('input')!r}，{error.get('msg', '')}"
        if allowed:
            text += f"；合法取值：{allowed}"
        parts.append(text)
    return "；".join(parts), details


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(AitraceError)
    async def _aitrace_error(_: Request, exc: AitraceError) -> JSONResponse:
        body: dict[str, Any] = {
            "code": exc.code,
            "message": exc.message,
            "retryable": exc.retryable,
        }
        if exc.retry_after is not None:
            body["retry_after"] = exc.retry_after
        return JSONResponse(status_code=exc.http_status, content=body)

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        message, details = describe_validation(exc.errors())
        return JSONResponse(
            status_code=400,
            content={
                "code": "INVALID_PAYLOAD",
                "message": message,
                "retryable": False,
                "details": details,
            },
        )
```

```python
# src/aitrace/api/deps.py
from __future__ import annotations

from collections.abc import Iterator

from fastapi import Request
from sqlalchemy.orm import Session

from aitrace.core.settings import Settings


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_session(request: Request) -> Iterator[Session]:
    with request.app.state.session_factory.begin() as session:
        yield session
```

```python
# src/aitrace/api/routes/health.py
from fastapi import APIRouter, Depends

from aitrace import __version__
from aitrace.api.deps import get_settings
from aitrace.core.settings import Settings

router = APIRouter(tags=["health"])


@router.get("/health")
def health(settings: Settings = Depends(get_settings)) -> dict[str, str]:
    return {"status": "ok", "version": __version__, "env": settings.env}
```

```python
# src/aitrace/api/app.py
from __future__ import annotations

from contextlib import asynccontextmanager
from collections.abc import AsyncIterator

from fastapi import FastAPI
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from aitrace.api.errors import install_error_handlers
from aitrace.api.routes import health
from aitrace.core.ids import SystemClock, UuidIdGenerator
from aitrace.core.settings import Settings, load_settings
from aitrace.profiles.loader import load_profiles
from aitrace.profiles.resolver import ProfileResolver


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()
    if settings.service_token is None:
        raise RuntimeError("service_token must be resolved before create_app")
    engine = create_engine(
        settings.database.dsn,
        pool_pre_ping=True,
        pool_size=settings.database.pool_size,
        max_overflow=0,
        connect_args={"options": f"-c statement_timeout={settings.database.statement_timeout_ms}"},
    )
    registry = load_profiles(settings.storage.profile_domains_dir)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        engine.dispose()

    app = FastAPI(
        title="AITrace",
        version="0.1.0",
        openapi_url="/api/v1/openapi.json",
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.engine = engine
    app.state.session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    app.state.profile_resolver = ProfileResolver(registry)
    app.state.clock = SystemClock()
    app.state.ids = UuidIdGenerator()
    install_error_handlers(app)
    app.include_router(health.router, prefix="/api/v1")
    return app
```

```python
# src/aitrace/main.py
import uvicorn

from aitrace.api.app import create_app


def run() -> None:
    app = create_app()
    settings = app.state.settings
    uvicorn.run(app, host=settings.server.host, port=settings.server.port)
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/unit/test_errors.py tests/unit/test_health.py -v`

Expected: 2 passed. `create_app` will fail here until Task 6 adds `profiles`; temporarily create the package in Task 6 before running? To keep Task 3 independently green, move profile loading to Task 9. For now, remove `load_profiles` from `create_app` and add it in Task 9. The app factory in Task 3 should only wire settings, engine, session factory, clock, and IDs.

Use this Task 3 version of `api/app.py` instead:

```python
from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from aitrace.api.errors import install_error_handlers
from aitrace.api.routes import health
from aitrace.core.ids import SystemClock, UuidIdGenerator
from aitrace.core.settings import Settings, load_settings


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()
    if settings.service_token is None:
        raise RuntimeError("service_token must be resolved before create_app")
    engine = create_engine(
        settings.database.dsn,
        pool_pre_ping=True,
        pool_size=settings.database.pool_size,
        max_overflow=0,
        connect_args={"options": f"-c statement_timeout={settings.database.statement_timeout_ms}"},
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        engine.dispose()

    app = FastAPI(
        title="AITrace",
        version="0.1.0",
        openapi_url="/api/v1/openapi.json",
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.engine = engine
    app.state.session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    app.state.clock = SystemClock()
    app.state.ids = UuidIdGenerator()
    install_error_handlers(app)
    app.include_router(health.router, prefix="/api/v1")
    return app
```

- [ ] **Step 5: Commit checkpoint**

```bash
git add src/aitrace/core src/aitrace/api src/aitrace/main.py tests
git commit -m "feat: add app factory, health endpoint and unified errors"
```

---

### Task 4: PostgreSQL schema and initial migration

**Files:**
- Create: `src/aitrace/persistence/__init__.py`
- Create: `src/aitrace/persistence/base.py`
- Create: `src/aitrace/persistence/models.py`
- Create: `alembic.ini`
- Create: `src/aitrace/persistence/migrations/env.py`
- Create: `src/aitrace/persistence/migrations/script.py.mako`
- Create: `src/aitrace/persistence/migrations/versions/0001_initial.py` (generated, then reviewed)
- Modify: `tests/conftest.py` (DB fixtures)
- Test: `tests/integration/test_schema.py`

**Interfaces:**
- Produces: SQLAlchemy models `SessionModel`, `Round`, `Report`, `Batch`, `TaskQueue`, `UserQueue`, `CaseLibrary`, `CaseContribution`, `MemoryHotFile`, `Profile`, `Attachment`, `AuditLog`; table names `session`, `round`, `report`, `batch`, `task_queue`, `user_queue`, `case_library`, `case_contribution`, `memory_hot_file`, `profile`, `attachment`, `audit_log`; `EPOCH`; `TaskQueueRow = TaskQueue`.

- [ ] **Step 1: Write the failing schema test**

```python
# tests/integration/test_schema.py
from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

CORE_TABLES = {
    "session",
    "round",
    "report",
    "batch",
    "task_queue",
    "user_queue",
    "case_library",
    "case_contribution",
    "memory_hot_file",
    "profile",
    "attachment",
    "audit_log",
}


def test_all_core_tables_exist(db_engine: Engine) -> None:
    assert CORE_TABLES <= set(inspect(db_engine).get_table_names())


def test_task_idempotency_indexes(db_engine: Engine) -> None:
    with db_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO session (session_id, product_id, batch_id, case_id, domain_id, "
                "profile_version, trigger_user_id, hardware_type, latest_round_no, status, "
                "env_topology, git_context, created_at) VALUES "
                "('S1','p','b','c','d','v','u','SSD',1,'RUNNING','{}','{}',now())"
            )
        )
        for task_id in ("T1", "T2"):
            conn.execute(
                text(
                    "INSERT INTO task_queue (task_id, batch_id, session_id, round_id, round_no, "
                    "product_id, case_id, task_type, queue_key, queue_state, priority, attempts, "
                    "max_attempts, lease_generation, lease_until, callback_status, payload, "
                    "created_at, updated_at) VALUES "
                    "(:task_id,'b','S1','R1',1,'p','c','case_clean','u','PENDING',1,0,3,0,"
                    "'epoch','PENDING','{}',now(),now())"
                ),
                {"task_id": task_id},
            )
    with db_engine.begin() as conn:
        try:
            conn.execute(
                text(
                    "INSERT INTO task_queue (task_id, batch_id, session_id, round_id, round_no, "
                    "product_id, case_id, task_type, queue_key, queue_state, priority, attempts, "
                    "max_attempts, lease_generation, lease_until, callback_status, payload, "
                    "created_at, updated_at) VALUES "
                    "('T3','b','S1','R1',1,'p','c','case_clean','u','PENDING',1,0,3,0,"
                    "'epoch','PENDING','{}',now(),now())"
                )
            )
        except IntegrityError:
            pass
        else:
            raise AssertionError("duplicate (session_id, round_no, task_type) must be rejected")
```

- [ ] **Step 2: Run and confirm failure**

Run: `TEST_DATABASE_URL=... pytest tests/integration/test_schema.py -v`

Expected: FAIL because tables do not exist.

- [ ] **Step 3: Implement base and models**

```python
# src/aitrace/persistence/base.py
from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.orm import DeclarativeBase

EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


class Base(DeclarativeBase):
    pass
```

```python
# src/aitrace/persistence/models.py
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from aitrace.persistence.base import EPOCH, Base


def _json_default() -> dict[str, Any]:
    return {}


class SessionModel(Base):
    __tablename__ = "session"
    __table_args__ = (
        Index("uq_session_delivery", "product_id", "batch_id", "case_id", unique=True),
    )

    session_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    product_id: Mapped[str] = mapped_column(String(64), nullable=False)
    batch_id: Mapped[str] = mapped_column(String(64), nullable=False)
    case_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    domain_id: Mapped[str] = mapped_column(String(64), nullable=False)
    profile_version: Mapped[str] = mapped_column(String(32), nullable=False)
    trigger_user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    hardware_type: Mapped[str] = mapped_column(String(16), nullable=False)
    stable_fingerprint: Mapped[str | None] = mapped_column(String(64))
    env_topology: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    git_context: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    latest_round_no: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="RUNNING")
    citation_group_id: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class Round(Base):
    __tablename__ = "round"
    __table_args__ = (Index("uq_round_no", "session_id", "round_no", unique=True),)

    round_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    session_id: Mapped[str] = mapped_column(
        ForeignKey("session.session_id", ondelete="CASCADE"), nullable=False
    )
    round_no: Mapped[int] = mapped_column(Integer, nullable=False)
    trigger_user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    queue_key: Mapped[str] = mapped_column(String(64), nullable=False)
    exact_fingerprint: Mapped[str | None] = mapped_column(String(64))
    stage: Mapped[str] = mapped_column(String(24), nullable=False, default="QUEUED")
    round_status: Mapped[str] = mapped_column(String(24), nullable=False, default="RUNNING")
    report_id: Mapped[str | None] = mapped_column(String(40))
    confidence: Mapped[float | None]
    feedback: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Report(Base):
    __tablename__ = "report"

    report_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    round_id: Mapped[str] = mapped_column(
        ForeignKey("round.round_id", ondelete="CASCADE"), nullable=False, unique=True
    )
    fault_info: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    result: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    confidence_breakdown: Mapped[list[Any]] = mapped_column(JSONB, nullable=False)
    evidence: Mapped[list[Any]] = mapped_column(JSONB, nullable=False)
    referenced_logs: Mapped[list[Any]] = mapped_column(JSONB, nullable=False)
    process_steps: Mapped[list[Any]] = mapped_column(JSONB, nullable=False)
    citation: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    gap_note: Mapped[str | None] = mapped_column(String(2048))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class Batch(Base):
    __tablename__ = "batch"

    batch_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    product_id: Mapped[str] = mapped_column(String(64), nullable=False)
    trigger_user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    batch_task_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    failed_cases: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    passed_cases: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    batch_state: Mapped[str] = mapped_column(String(16), nullable=False, default="RUNNING")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class TaskQueue(Base):
    __tablename__ = "task_queue"
    __table_args__ = (
        Index("ix_task_dispatch", "queue_key", "queue_state", "priority", "created_at"),
        Index("ix_task_lease", "queue_state", "lease_until", "priority"),
        Index(
            "uq_task_case",
            "session_id",
            "round_no",
            "task_type",
            unique=True,
            postgresql_where=text("case_id <> ''"),
        ),
        Index(
            "uq_task_batch",
            "batch_id",
            "task_type",
            unique=True,
            postgresql_where=text("case_id = ''"),
        ),
    )

    task_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    batch_id: Mapped[str] = mapped_column(String(64), nullable=False)
    session_id: Mapped[str] = mapped_column(
        ForeignKey("session.session_id", ondelete="CASCADE"), nullable=False
    )
    round_id: Mapped[str] = mapped_column(String(40), nullable=False)
    round_no: Mapped[int] = mapped_column(Integer, nullable=False)
    product_id: Mapped[str] = mapped_column(String(64), nullable=False)
    case_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    task_type: Mapped[str] = mapped_column(String(32), nullable=False)
    queue_key: Mapped[str] = mapped_column(String(64), nullable=False)
    queue_state: Mapped[str] = mapped_column(String(16), nullable=False, default="PENDING")
    dispatch_phase: Mapped[str | None] = mapped_column(String(16))
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    depends_on_task_id: Mapped[str | None] = mapped_column(String(40))
    not_before: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    lease_owner: Mapped[str | None] = mapped_column(String(64))
    lease_generation: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    lease_until: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=EPOCH, server_default=text("'epoch'")
    )
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    callback_status: Mapped[str] = mapped_column(String(16), nullable=False, default="PENDING")
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=_json_default, server_default=text("'{}'::jsonb")
    )
    error: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class UserQueue(Base):
    __tablename__ = "user_queue"
    __table_args__ = (CheckConstraint("in_flight >= 0", name="ck_user_queue_in_flight"),)

    queue_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="ACTIVE")
    in_flight: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    paused_until: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=EPOCH, server_default=text("'epoch'")
    )
    last_error: Mapped[str | None] = mapped_column(String(255))
    last_dispatch_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class CaseLibrary(Base):
    __tablename__ = "case_library"

    domain_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    stable_fingerprint: Mapped[str] = mapped_column(String(64), primary_key=True)
    category: Mapped[str] = mapped_column(String(32), nullable=False)
    ai_conclusion: Mapped[str] = mapped_column(String(4096), nullable=False)
    correction: Mapped[str | None] = mapped_column(String(4096))
    evidence_summary: Mapped[str] = mapped_column(String(4096), nullable=False)
    exact_fingerprints: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    confirm_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    use_hits: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    observation_hits: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    entry_status: Mapped[str] = mapped_column(String(16), nullable=False, default="observing")
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class CaseContribution(Base):
    __tablename__ = "case_contribution"

    domain_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    stable_fingerprint: Mapped[str] = mapped_column(String(64), primary_key=True)
    session_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    round_no: Mapped[int] = mapped_column(Integer, primary_key=True)
    feedback_version: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    report_id: Mapped[str] = mapped_column(String(40), nullable=False)
    category: Mapped[str] = mapped_column(String(32), nullable=False)
    ai_conclusion: Mapped[str] = mapped_column(String(4096), nullable=False)
    correction: Mapped[str | None] = mapped_column(String(4096))
    evidence_summary: Mapped[str] = mapped_column(String(4096), nullable=False)
    observation_hits: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class MemoryHotFile(Base):
    __tablename__ = "memory_hot_file"

    domain_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    entries: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class Profile(Base):
    __tablename__ = "profile"

    domain_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    config: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    version: Mapped[str] = mapped_column(String(32), nullable=False)
    updated_by: Mapped[str] = mapped_column(String(64), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class Attachment(Base):
    __tablename__ = "attachment"

    attachment_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    session_id: Mapped[str] = mapped_column(
        ForeignKey("session.session_id", ondelete="CASCADE"), nullable=False
    )
    round_no: Mapped[int] = mapped_column(Integer, nullable=False)
    media_type: Mapped[str] = mapped_column(String(128), nullable=False)
    path: Mapped[str] = mapped_column(String(1024), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    uploaded_by: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    actor: Mapped[str] = mapped_column(String(64), nullable=False)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    target: Mapped[str] = mapped_column(String(255), nullable=False)
    detail: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=_json_default, server_default=text("'{}'::jsonb")
    )
    prev_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    hash: Mapped[str] = mapped_column(String(64), nullable=False)


TaskQueueRow = TaskQueue
```

- [ ] **Step 4: Configure Alembic and generate the initial revision**

```ini
# alembic.ini
[alembic]
script_location = src/aitrace/persistence/migrations
prepend_sys_path = .
version_path_separator = os
```

```python
# src/aitrace/persistence/migrations/env.py
from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine

from aitrace.persistence.base import Base
from aitrace.persistence import models  # noqa: F401

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)


def _url() -> str:
    override = context.get_x_argument(as_dictionary=True).get("db_url")
    return override or os.environ["AITRACE_DATABASE__DSN"]


def run_migrations_offline() -> None:
    context.configure(url=_url(), target_metadata=Base.metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(_url(), pool_pre_ping=True)
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=Base.metadata)
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
```

```mako
# src/aitrace/persistence/migrations/script.py.mako
"""${message}

Revision ID: ${up_revision}
Revises: ${down_revision | comma,n}
Create Date: ${create_date}
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
${imports if imports else ""}

revision = ${repr(up_revision)}
down_revision = ${repr(down_revision)}
branch_labels = ${repr(branch_labels)}
depends_on = ${repr(depends_on)}


def upgrade() -> None:
    ${upgrades if upgrades else "pass"}


def downgrade() -> None:
    ${downgrades if downgrades else "pass"}
```

Generate the revision:

```bash
AITRACE_DATABASE__DSN="$TEST_DATABASE_URL" \
  .venv/bin/alembic revision --autogenerate -m "initial schema" --rev-id 0001
```

Review `0001_initial.py` and confirm all twelve tables, partial unique indexes `uq_task_case`/`uq_task_batch`, `lease_until` server default `'epoch'`, and `uq_session_delivery` exist. Append the append-only trigger to the generated revision:

```python
def upgrade() -> None:
    # ... generated operations above ...
    op.execute(
        """
        CREATE OR REPLACE FUNCTION aitrace_audit_immutable() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'audit_log is append-only';
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        "CREATE TRIGGER audit_log_no_update BEFORE UPDATE OR DELETE ON audit_log "
        "FOR EACH ROW EXECUTE FUNCTION aitrace_audit_immutable();"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS audit_log_no_update ON audit_log")
    op.execute("DROP FUNCTION IF EXISTS aitrace_audit_immutable()")
    # ... generated downgrade operations ...
```

- [ ] **Step 5: Add DB fixtures**

```python
# tests/conftest.py (append)
import subprocess
import sys

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

requires_db = pytest.mark.skipif(TEST_DATABASE_URL is None, reason="TEST_DATABASE_URL is not set")

_TRUNCATE = """
TRUNCATE TABLE audit_log, attachment, profile, memory_hot_file, case_contribution,
case_library, user_queue, task_queue, batch, report, "round", "session"
RESTART IDENTITY CASCADE
"""


@pytest.fixture(scope="session")
def db_engine() -> Engine:
    if TEST_DATABASE_URL is None:
        pytest.skip("TEST_DATABASE_URL is not set")
    subprocess.run(
        [sys.executable, "-m", "alembic", "-x", f"db_url={TEST_DATABASE_URL}", "upgrade", "head"],
        check=True,
    )
    engine = create_engine(TEST_DATABASE_URL)
    yield engine
    engine.dispose()


@pytest.fixture
def db_session(db_engine: Engine) -> Session:
    factory = sessionmaker(bind=db_engine, expire_on_commit=False)
    with factory() as session:
        session.execute(text(_TRUNCATE))
        session.commit()
        yield session
        session.rollback()
```

- [ ] **Step 6: Run schema tests**

Run: `TEST_DATABASE_URL=postgresql+psycopg://aitrace:aitrace@127.0.0.1:5432/aitrace_test pytest tests/integration/test_schema.py -v`

Expected: 2 passed.

- [ ] **Step 7: Commit checkpoint**

```bash
git add src/aitrace/persistence alembic.ini tests/conftest.py tests/integration/test_schema.py
git commit -m "feat: add V1 schema and initial migration"
```

---

### Task 5: Append-only audit hash chain

**Files:**
- Create: `src/aitrace/core/audit.py`
- Test: `tests/integration/test_audit.py`

**Interfaces:**
- Produces: `AuditService(session_factory)` with `append(actor: str, action: str, target: str, detail: dict, now: datetime | None = None) -> str` and `verify_chain() -> bool`; `GENESIS_HASH`.

- [ ] **Step 1: Write the failing audit tests**

```python
# tests/integration/test_audit.py
from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import sessionmaker

from aitrace.core.audit import GENESIS_HASH, AuditService


def test_appends_form_a_valid_chain(db_engine: Engine) -> None:
    service = AuditService(sessionmaker(bind=db_engine, expire_on_commit=False))
    first = service.append(
        actor="zhangsan", action="token.fetch", target="p/b/c", detail={"result": "ok"}
    )
    second = service.append(
        actor="zhangsan", action="token.fetch", target="p/b/c", detail={"result": "expired"}
    )
    assert first != second
    assert service.verify_chain() is True

    with db_engine.connect() as conn:
        rows = conn.execute(
            text("SELECT prev_hash, hash FROM audit_log ORDER BY id")
        ).all()
    assert rows[0].prev_hash == GENESIS_HASH
    assert rows[1].prev_hash == rows[0].hash


def test_update_is_rejected_by_database_trigger(db_engine: Engine) -> None:
    service = AuditService(sessionmaker(bind=db_engine, expire_on_commit=False))
    service.append(actor="a", action="x", target="t", detail={})
    with pytest.raises(DBAPIError) as caught:
        with db_engine.begin() as conn:
            conn.execute(text("UPDATE audit_log SET actor='evil' WHERE id=1"))
    assert "append-only" in str(caught.value)


def test_verify_detects_a_forged_row(db_engine: Engine) -> None:
    service = AuditService(sessionmaker(bind=db_engine, expire_on_commit=False))
    service.append(actor="a", action="x", target="t", detail={})
    with db_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO audit_log (ts, actor, action, target, detail, prev_hash, hash) "
                "VALUES (:ts, 'forger', 'x', 't', '{}'::jsonb, :prev, repeat('0', 64))"
            ),
            {"ts": datetime.now(UTC), "prev": "0" * 64},
        )
    assert service.verify_chain() is False
```

- [ ] **Step 2: Run and confirm failure**

Run: `pytest tests/integration/test_audit.py -v`

Expected: FAIL with `ModuleNotFoundError: No module named 'aitrace.core.audit'`.

- [ ] **Step 3: Implement the audit service**

```python
# src/aitrace/core/audit.py
from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.orm import Session, sessionmaker

from aitrace.persistence.models import AuditLog

GENESIS_HASH = "0" * 64
_CHAIN_LOCK_KEY = 0x41495452  # "AITR"


def _canonical(ts: datetime, actor: str, action: str, target: str, detail: dict[str, Any]) -> str:
    return json.dumps(
        {
            "ts": ts.astimezone(UTC).isoformat(timespec="microseconds"),
            "actor": actor,
            "action": action,
            "target": target,
            "detail": detail,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def _digest(prev_hash: str, payload: str) -> str:
    return hashlib.sha256((prev_hash + payload).encode("utf-8")).hexdigest()


class AuditService:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def append(
        self,
        *,
        actor: str,
        action: str,
        target: str,
        detail: dict[str, Any],
        now: datetime | None = None,
    ) -> str:
        ts = (now or datetime.now(UTC)).astimezone(UTC)
        with self._session_factory.begin() as session:
            session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": _CHAIN_LOCK_KEY})
            prev_hash = session.execute(
                select(AuditLog.hash).order_by(AuditLog.id.desc()).limit(1)
            ).scalar_one_or_none() or GENESIS_HASH
            digest = _digest(prev_hash, _canonical(ts, actor, action, target, detail))
            session.add(
                AuditLog(
                    ts=ts,
                    actor=actor,
                    action=action,
                    target=target,
                    detail=detail,
                    prev_hash=prev_hash,
                    hash=digest,
                )
            )
            return digest

    def verify_chain(self) -> bool:
        with self._session_factory() as session:
            prev_hash = GENESIS_HASH
            for row in session.execute(select(AuditLog).order_by(AuditLog.id)).scalars():
                if row.prev_hash != prev_hash:
                    return False
                expected = _digest(
                    prev_hash, _canonical(row.ts, row.actor, row.action, row.target, row.detail)
                )
                if row.hash != expected:
                    return False
                prev_hash = row.hash
            return True
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/integration/test_audit.py -v`

Expected: 3 passed.

- [ ] **Step 5: Commit checkpoint**

```bash
git add src/aitrace/core/audit.py tests/integration/test_audit.py
git commit -m "feat: add append-only audit hash chain"
```

---

### Task 6: Domain profile schema, loader, and resolver

**Files:**
- Create: `src/aitrace/profiles/__init__.py`
- Create: `src/aitrace/profiles/models.py`
- Create: `src/aitrace/profiles/loader.py`
- Create: `src/aitrace/profiles/resolver.py`
- Create: `deploy/config/domains/registry.yaml`
- Create: `deploy/config/domains/default.yaml`
- Create: `deploy/config/domains/enterprise.yaml`
- Create: `deploy/config/domains/consumer.yaml`
- Create: `deploy/config/domains/cxl.yaml`
- Test: `tests/unit/test_profiles.py`

**Interfaces:**
- Produces: `DomainProfile`, `ProfileRegistry`, `ProfileResolver.resolve(product_id: str | None) -> ResolvedProfile`, `ResolvedProfile(domain_id, version, config, product_id)`, `load_profiles(domains_dir: Path) -> ProfileRegistry`, `profile_version(profile) -> str`.

- [ ] **Step 1: Write the failing profile tests**

```python
# tests/unit/test_profiles.py
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from aitrace.profiles.loader import ProfileConfigError, load_profiles
from aitrace.profiles.models import DomainProfile
from aitrace.profiles.resolver import ProfileResolver, profile_version

DOMAINS = Path("deploy/config/domains")


def test_default_profile_is_loaded() -> None:
    registry = load_profiles(DOMAINS)
    assert registry.default_profile.domain_id == "default"
    assert registry.default_profile.root_cause_enum[:5] == (
        "TEST_ENV_ERROR",
        "FW_DEFECT",
        "SCRIPT_ISSUE",
        "HW_DEFECT",
        "OTHER",
    )


def test_product_resolves_to_its_domain() -> None:
    resolved = ProfileResolver(load_profiles(DOMAINS)).resolve("ssd_9560")
    assert resolved.domain_id == "enterprise"
    assert resolved.product_id == "ssd_9560"
    assert resolved.version == profile_version(resolved.config)


def test_unknown_product_falls_back_to_default() -> None:
    resolved = ProfileResolver(load_profiles(DOMAINS)).resolve("unknown_product")
    assert resolved.domain_id == "default"


def test_duplicate_product_across_domains_is_rejected(tmp_path: Path) -> None:
    (tmp_path / "default.yaml").write_text(DOMAINS.joinpath("default.yaml").read_text())
    for name in ("enterprise", "consumer", "cxl"):
        (tmp_path / f"{name}.yaml").write_text(DOMAINS.joinpath(f"{name}.yaml").read_text())
    data = yaml.safe_load((DOMAINS / "consumer.yaml").read_text())
    data["product_ids"] = ["ssd_9560"]
    (tmp_path / "consumer.yaml").write_text(yaml.safe_dump(data, allow_unicode=True))
    (tmp_path / "registry.yaml").write_text((DOMAINS / "registry.yaml").read_text())
    with pytest.raises(ProfileConfigError) as caught:
        load_profiles(tmp_path)
    assert "重复" in str(caught.value)


def test_write_command_is_rejected_from_probe_whitelist() -> None:
    data = yaml.safe_load((DOMAINS / "enterprise.yaml").read_text())
    data["probe_whitelist"] = ["nvme format /dev/nvme0"]
    with pytest.raises(ValidationError) as caught:
        DomainProfile.model_validate(data)
    assert "只读" in str(caught.value)


def test_confidence_weights_must_sum_to_one() -> None:
    data = yaml.safe_load((DOMAINS / "enterprise.yaml").read_text())
    data["confidence_weights"]["reference_hit"] = 0.5
    with pytest.raises(ValidationError) as caught:
        DomainProfile.model_validate(data)
    assert "1.0" in str(caught.value)


def test_version_changes_when_profile_changes() -> None:
    base = DomainProfile.model_validate(yaml.safe_load((DOMAINS / "enterprise.yaml").read_text()))
    changed = base.model_copy(update={"analysis_focus": "不同的分析侧重"})
    assert profile_version(base) != profile_version(changed)
```

- [ ] **Step 2: Run and confirm failure**

Run: `pytest tests/unit/test_profiles.py -v`

Expected: FAIL with `ModuleNotFoundError: No module named 'aitrace.profiles'`.

- [ ] **Step 3: Implement profile models**

```python
# src/aitrace/profiles/models.py
from __future__ import annotations

import re
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

DEFAULT_ROOT_CAUSES = ("TEST_ENV_ERROR", "FW_DEFECT", "SCRIPT_ISSUE", "HW_DEFECT", "OTHER")
_FORBIDDEN_PROBE = re.compile(
    r"(?i)\b(format|fw-download|fw-commit|reset|delete|write|sanitize|set-feature)\b"
)


class LogSource(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    name: Literal["test", "uart", "dmesg", "scheduler"]
    plugin: str = Field(min_length=1)


class SliceParams(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    baseline_lines: int = Field(default=200, ge=0)
    first_error_before_s: int = Field(default=60, ge=0)
    first_error_after_s: int = Field(default=15, ge=0)
    tail_lines: int = Field(default=100, ge=0)
    small_log_full_threshold_bytes: int = Field(default=1024 * 1024, ge=0)
    first_error_window_budget_bytes: int = Field(default=5 * 1024 * 1024, ge=0)


class ConfidenceWeights(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    log_completeness: float = Field(default=0.15, ge=0, le=1)
    time_alignment: float = Field(default=0.15, ge=0, le=1)
    error_info_completeness: float = Field(default=0.10, ge=0, le=1)
    process_error_rate: float = Field(default=0.10, ge=0, le=1)
    evidence_chain: float = Field(default=0.15, ge=0, le=1)
    reference_hit: float = Field(default=0.20, ge=0, le=1)
    consistency_check: float = Field(default=0.15, ge=0, le=1)

    @model_validator(mode="after")
    def _sum_to_one(self) -> Self:
        total = sum(self.model_dump().values())
        if abs(total - 1.0) > 1e-6:
            raise ValueError(f"置信度权重之和必须为 1.0，当前为 {total:.3f}")
        return self


class DomainProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    domain_id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    product_ids: tuple[str, ...] = ()
    log_sources: tuple[LogSource, ...]
    cleaning_rules: dict[str, Any] = Field(default_factory=dict)
    anchor_keywords: dict[str, tuple[str, ...]]
    slice_params: SliceParams = Field(default_factory=SliceParams)
    confidence_weights: ConfidenceWeights = Field(default_factory=ConfidenceWeights)
    analysis_focus: str = Field(min_length=1)
    prompt_template: str = ""
    skills_namespace: str
    memory_namespace: str
    root_cause_enum: tuple[str, ...] = DEFAULT_ROOT_CAUSES
    probe_whitelist: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _validate(self) -> Self:
        names = [source.name for source in self.log_sources]
        if len(names) != len(set(names)):
            raise ValueError("日志源重复：log_sources 中同一 name 只能出现一次")
        if not set(DEFAULT_ROOT_CAUSES).issubset(self.root_cause_enum):
            raise ValueError(f"根因闭集必须包含默认五类：{DEFAULT_ROOT_CAUSES}")
        if self.skills_namespace != f"{self.domain_id}_":
            raise ValueError(f"skills_namespace 必须为 {self.domain_id}_")
        if self.memory_namespace != f"domain:{self.domain_id}:":
            raise ValueError(f"memory_namespace 必须为 domain:{self.domain_id}:")
        for command in self.probe_whitelist:
            if _FORBIDDEN_PROBE.search(command):
                raise ValueError(f"探针白名单只允许只读命令，非法命令：{command}")
        return self
```

- [ ] **Step 4: Implement loader and resolver**

```python
# src/aitrace/profiles/loader.py
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Mapping

import yaml
from pydantic import BaseModel, ConfigDict, ValidationError

from aitrace.profiles.models import DomainProfile


class ProfileConfigError(RuntimeError):
    pass


class RegistryFile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: int = 1
    default: str
    domains: dict[str, str]


@dataclass(frozen=True)
class ProfileRegistry:
    default_profile: DomainProfile
    profiles: Mapping[str, DomainProfile]
    product_index: Mapping[str, str]


def load_profiles(domains_dir: Path) -> ProfileRegistry:
    registry_path = domains_dir / "registry.yaml"
    if not registry_path.is_file():
        raise ProfileConfigError(f"领域画像注册表不存在：{registry_path}")
    try:
        registry = RegistryFile.model_validate(
            yaml.safe_load(registry_path.read_text(encoding="utf-8"))
        )
    except (ValidationError, yaml.YAMLError) as exc:
        raise ProfileConfigError(f"领域画像注册表校验失败：{exc}") from exc

    profiles: dict[str, DomainProfile] = {}
    for domain_id, filename in registry.domains.items():
        path = domains_dir / filename
        if not path.is_file():
            raise ProfileConfigError(f"领域画像文件不存在：{path}")
        try:
            profile = DomainProfile.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
        except (ValidationError, yaml.YAMLError) as exc:
            raise ProfileConfigError(f"领域画像 {domain_id} 校验失败：{exc}") from exc
        if profile.domain_id != domain_id:
            raise ProfileConfigError(
                f"领域画像 id 不一致：注册键 {domain_id}，文件内 {profile.domain_id}"
            )
        profiles[domain_id] = profile

    if registry.default not in profiles:
        raise ProfileConfigError(f"默认画像 {registry.default} 未在 domains 中定义")

    product_index: dict[str, str] = {}
    for domain_id, profile in profiles.items():
        for product_id in profile.product_ids:
            if product_id in product_index:
                raise ProfileConfigError(
                    f"产品 {product_id} 重复归属领域：{product_index[product_id]} 与 {domain_id}"
                )
            product_index[product_id] = domain_id

    return ProfileRegistry(
        default_profile=profiles[registry.default],
        profiles=MappingProxyType(profiles),
        product_index=MappingProxyType(product_index),
    )
```

```python
# src/aitrace/profiles/resolver.py
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from aitrace.profiles.loader import ProfileRegistry
from aitrace.profiles.models import DomainProfile


@dataclass(frozen=True)
class ResolvedProfile:
    domain_id: str
    version: str
    config: DomainProfile
    product_id: str | None


def profile_version(profile: DomainProfile) -> str:
    payload = json.dumps(
        profile.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


class ProfileResolver:
    def __init__(self, registry: ProfileRegistry) -> None:
        self._registry = registry

    def resolve(self, product_id: str | None) -> ResolvedProfile:
        domain_id = self._registry.product_index.get(product_id or "", self._registry.default_profile.domain_id)
        profile = self._registry.profiles[domain_id]
        return ResolvedProfile(
            domain_id=domain_id,
            version=profile_version(profile),
            config=profile,
            product_id=product_id,
        )
```

- [ ] **Step 5: Add shipped domain profiles**

`registry.yaml`:

```yaml
version: 1
default: default
domains:
  default: default.yaml
  enterprise: enterprise.yaml
  consumer: consumer.yaml
  cxl: cxl.yaml
```

`default.yaml`:

```yaml
domain_id: default
product_ids: []
log_sources:
  - {name: test, plugin: test_log}
  - {name: uart, plugin: uart_log}
  - {name: dmesg, plugin: dmesg_log}
  - {name: scheduler, plugin: scheduler_log}
cleaning_rules: {}
anchor_keywords:
  test: ["Traceback", "ERROR", "FAILED"]
  uart: ["ASSERT", "PANIC", "WATCHDOG", "RESET"]
  dmesg: ["Link Down", "AER", "Controller Reset", "Call Trace"]
  scheduler: ["TIMEOUT", "LOST", "TERMINATED"]
slice_params: {}
confidence_weights: {}
analysis_focus: "通用失败定界：证据完整性与时间线一致性优先"
prompt_template: ""
skills_namespace: default_
memory_namespace: "domain:default:"
root_cause_enum: [TEST_ENV_ERROR, FW_DEFECT, SCRIPT_ISSUE, HW_DEFECT, OTHER]
probe_whitelist: []
```

`enterprise.yaml`:

```yaml
domain_id: enterprise
product_ids: [ssd_8560, ssd_9560, ssd_6100, ssd_3100]
log_sources:
  - {name: test, plugin: test_log}
  - {name: uart, plugin: uart_log}
  - {name: dmesg, plugin: dmesg_log}
  - {name: scheduler, plugin: scheduler_log}
cleaning_rules:
  heartbeat_fold_seconds: 60
  perf_sample_downsample_seconds: 60
anchor_keywords:
  test: ["Traceback", "ERROR", "FAILED"]
  uart: ["ASSERT", "PANIC", "WATCHDOG", "RESET", "TIMEOUT"]
  dmesg: ["Link Down", "AER", "Controller Reset", "Call Trace", "nvme"]
  scheduler: ["TIMEOUT", "LOST", "TERMINATED"]
slice_params:
  first_error_before_s: 60
  first_error_after_s: 15
confidence_weights: {}
analysis_focus: "企业级 SSD：掉盘、数据一致性与固件死锁优先"
skills_namespace: enterprise_
memory_namespace: "domain:enterprise:"
root_cause_enum: [TEST_ENV_ERROR, FW_DEFECT, SCRIPT_ISSUE, HW_DEFECT, OTHER]
probe_whitelist:
  - "nvme list"
  - "nvme id-ctrl"
  - "lspci -vv"
  - "smartctl -a"
```

`consumer.yaml`:

```yaml
domain_id: consumer
product_ids: [ufs]
log_sources:
  - {name: test, plugin: test_log}
  - {name: dmesg, plugin: dmesg_log}
anchor_keywords:
  test: ["Traceback", "ERROR", "FAILED"]
  dmesg: ["ufs", "link", "timeout"]
analysis_focus: "消费级：UFS 链路与低功耗异常优先"
skills_namespace: consumer_
memory_namespace: "domain:consumer:"
root_cause_enum: [TEST_ENV_ERROR, FW_DEFECT, SCRIPT_ISSUE, HW_DEFECT, OTHER]
probe_whitelist: ["lspci -vv", "dmesg --read"]
```

`cxl.yaml`:

```yaml
domain_id: cxl
product_ids: [cxl_1, cxl_2]
log_sources:
  - {name: test, plugin: test_log}
  - {name: dmesg, plugin: dmesg_log}
  - {name: scheduler, plugin: scheduler_log}
anchor_keywords:
  test: ["Traceback", "ERROR", "FAILED"]
  dmesg: ["CXL", "protocol", "Link Down", "AER"]
  scheduler: ["TIMEOUT", "LOST"]
analysis_focus: "CXL 协议一致性与链路异常优先"
skills_namespace: cxl_
memory_namespace: "domain:cxl:"
root_cause_enum: [TEST_ENV_ERROR, FW_DEFECT, SCRIPT_ISSUE, HW_DEFECT, OTHER]
probe_whitelist: ["cxl list", "lspci -vv", "dmesg --read"]
```

- [ ] **Step 6: Run tests**

Run: `pytest tests/unit/test_profiles.py -v`

Expected: 7 passed.

- [ ] **Step 7: Commit checkpoint**

```bash
git add src/aitrace/profiles deploy/config/domains tests/unit/test_profiles.py
git commit -m "feat: add domain profile schema, registry and resolver"
```

---

### Task 7: DB queue enqueue, cleaning claim, fencing, retry, and reclaim

**Files:**
- Create: `src/aitrace/scheduling/__init__.py`
- Create: `src/aitrace/scheduling/types.py`
- Create: `src/aitrace/persistence/repositories/__init__.py`
- Create: `src/aitrace/persistence/repositories/queue.py`
- Test: `tests/integration/test_queue_lease.py`

**Interfaces:**
- Produces: `NewTask`; `QueueRepository(session, lease_seconds=600)` with `ensure_user_queue(queue_key, now)`, `enqueue(task: NewTask, now) -> TaskQueueRow`, `claim_clean(owner, now) -> TaskQueueRow | None`, `finish(task_id, owner, generation, state, now, error=None) -> bool`, `retry(task_id, owner, generation, now, error) -> QueueState`, `reclaim_confirmed(task_id, owner, generation, now) -> bool`.

- [ ] **Step 1: Write the failing queue tests**

```python
# tests/integration/test_queue_lease.py
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from aitrace.core.enums import QueueState, TaskType
from aitrace.persistence.repositories.queue import QueueRepository
from aitrace.scheduling.types import NewTask

NOW = datetime(2026, 11, 25, 12, 0, tzinfo=UTC)


def _session(factory: sessionmaker[Session]) -> Session:
    return factory()


def _task(task_id: str = "T1") -> NewTask:
    return NewTask(
        task_id=task_id,
        batch_id="B1",
        session_id="S1",
        round_id="R1",
        round_no=1,
        product_id="ssd_9560",
        case_id="C1",
        task_type=TaskType.CASE_CLEAN,
        queue_key="zhangsan",
    )


@pytest.fixture
def factory(db_engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=db_engine, expire_on_commit=False)


@pytest.fixture
def seeded(db_session: Session) -> Session:
    db_session.execute(
        text(
            "INSERT INTO session (session_id, product_id, batch_id, case_id, domain_id, "
            "profile_version, trigger_user_id, hardware_type, latest_round_no, status, "
            "env_topology, git_context, created_at) VALUES "
            "('S1','ssd_9560','B1','C1','enterprise','v1','zhangsan','SSD',1,'RUNNING',"
            "'{}','{}',now())"
        )
    )
    db_session.commit()
    return db_session


def test_enqueue_creates_pending_task_and_active_queue(seeded: Session) -> None:
    repo = QueueRepository(seeded)
    row = repo.enqueue(_task(), NOW)
    seeded.commit()
    assert row.queue_state == QueueState.PENDING
    assert row.lease_generation == 0
    state = seeded.execute(
        text("SELECT state, in_flight FROM user_queue WHERE queue_key='zhangsan'")
    ).one()
    assert state == ("ACTIVE", 0)


def test_clean_claim_uses_lease_and_fences_old_owner(
    seeded: Session, factory: sessionmaker[Session]
) -> None:
    repo = QueueRepository(seeded)
    repo.enqueue(_task(), NOW)
    seeded.commit()

    claimed = repo.claim_clean("worker-a", NOW)
    seeded.commit()
    assert claimed is not None
    assert claimed.queue_state == QueueState.CLEANING
    assert claimed.lease_owner == "worker-a"
    assert claimed.lease_generation == 1

    stale = repo.finish("T1", "worker-b", 1, QueueState.DONE, NOW)
    assert stale is False

    assert repo.finish("T1", "worker-a", 1, QueueState.DONE, NOW) is True
    seeded.commit()
    assert seeded.execute(text("SELECT queue_state FROM task_queue WHERE task_id='T1'")).scalar_one() == "DONE"


def test_retry_returns_task_to_pending_with_backoff(seeded: Session) -> None:
    repo = QueueRepository(seeded)
    repo.enqueue(_task(), NOW)
    seeded.commit()
    repo.claim_clean("worker-a", NOW)
    seeded.commit()

    state = repo.retry("T1", "worker-a", 1, NOW, {"code": "IO_ERROR", "message": "读取失败"})
    seeded.commit()
    row = seeded.execute(text("SELECT queue_state, attempts, next_retry_at FROM task_queue WHERE task_id='T1'")).one()
    assert state == QueueState.PENDING
    assert row.queue_state == "PENDING"
    assert row.attempts == 1
    assert row.next_retry_at > NOW


def test_retry_exhaustion_fails_task(seeded: Session) -> None:
    repo = QueueRepository(seeded)
    repo.enqueue(_task(), NOW)
    seeded.commit()
    repo.claim_clean("worker-a", NOW)
    seeded.commit()
    seeded.execute(text("UPDATE task_queue SET attempts=2, max_attempts=3 WHERE task_id='T1'"))
    seeded.commit()
    state = repo.retry("T1", "worker-a", 1, NOW, {"code": "IO_ERROR", "message": "读取失败"})
    seeded.commit()
    assert state == QueueState.FAILED
```

- [ ] **Step 2: Run and confirm failure**

Run: `pytest tests/integration/test_queue_lease.py -v`

Expected: FAIL with `ModuleNotFoundError: No module named 'aitrace.scheduling'`.

- [ ] **Step 3: Implement task type and queue repository**

```python
# src/aitrace/scheduling/types.py
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from aitrace.core.enums import TaskType


@dataclass(frozen=True)
class NewTask:
    task_id: str
    batch_id: str
    session_id: str
    round_id: str
    round_no: int
    product_id: str
    case_id: str
    task_type: TaskType
    queue_key: str
    priority: int = 1
    max_attempts: int = 3
    payload: dict[str, Any] = field(default_factory=dict)
```

```python
# src/aitrace/persistence/repositories/queue.py
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import func, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from aitrace.core.enums import QueueState, TaskType
from aitrace.persistence.base import EPOCH
from aitrace.persistence.models import TaskQueue, TaskQueueRow, UserQueue
from aitrace.scheduling.types import NewTask


class QueueRepository:
    def __init__(self, session: Session, *, lease_seconds: int = 600) -> None:
        self._session = session
        self._lease = timedelta(seconds=lease_seconds)

    def ensure_user_queue(self, queue_key: str, now: datetime) -> None:
        self._session.execute(
            insert(UserQueue)
            .values(
                queue_key=queue_key,
                state="ACTIVE",
                in_flight=0,
                paused_until=EPOCH,
                last_dispatch_at=None,
                updated_at=now,
            )
            .on_conflict_do_nothing(index_elements=["queue_key"])
        )

    def enqueue(self, task: NewTask, now: datetime) -> TaskQueueRow:
        self.ensure_user_queue(task.queue_key, now)
        row = TaskQueue(
            task_id=task.task_id,
            batch_id=task.batch_id,
            session_id=task.session_id,
            round_id=task.round_id,
            round_no=task.round_no,
            product_id=task.product_id,
            case_id=task.case_id,
            task_type=str(task.task_type),
            queue_key=task.queue_key,
            queue_state=QueueState.PENDING,
            dispatch_phase=None,
            priority=task.priority,
            depends_on_task_id=None,
            not_before=None,
            attempts=0,
            max_attempts=task.max_attempts,
            lease_owner=None,
            lease_generation=0,
            lease_until=EPOCH,
            next_retry_at=None,
            callback_status="PENDING",
            payload=task.payload,
            error=None,
            created_at=now,
            updated_at=now,
        )
        self._session.add(row)
        self._session.flush()
        return row

    def claim_clean(self, owner: str, now: datetime) -> TaskQueueRow | None:
        row = self._session.execute(
            select(TaskQueue)
            .where(
                TaskQueue.queue_state == QueueState.PENDING,
                TaskQueue.task_type == TaskType.CASE_CLEAN,
                TaskQueue.lease_until < now,
                or_(TaskQueue.not_before.is_(None), TaskQueue.not_before <= now),
                or_(TaskQueue.next_retry_at.is_(None), TaskQueue.next_retry_at <= now),
            )
            .order_by(TaskQueue.priority.asc(), TaskQueue.created_at.asc())
            .with_for_update(skip_locked=True)
            .limit(1)
        ).scalar_one_or_none()
        if row is None:
            return None
        row.lease_owner = owner
        row.lease_generation += 1
        row.lease_until = now + self._lease
        row.queue_state = QueueState.CLEANING
        row.updated_at = now
        self._session.flush()
        return row

    def finish(
        self,
        task_id: str,
        owner: str,
        generation: int,
        state: QueueState,
        now: datetime,
        error: dict[str, object] | None = None,
    ) -> bool:
        row = self._session.execute(
            select(TaskQueue).where(TaskQueue.task_id == task_id).with_for_update()
        ).scalar_one_or_none()
        if row is None or row.lease_owner != owner or row.lease_generation != generation:
            return False
        was_ai = row.queue_state == QueueState.AI_RUNNING
        row.queue_state = state
        row.dispatch_phase = None
        row.lease_owner = None
        row.lease_until = EPOCH
        row.error = error
        row.updated_at = now
        if was_ai:
            self._release_slot(row.queue_key, now)
        self._session.flush()
        return True

    def retry(
        self,
        task_id: str,
        owner: str,
        generation: int,
        now: datetime,
        error: dict[str, object],
    ) -> QueueState:
        row = self._session.execute(
            select(TaskQueue).where(TaskQueue.task_id == task_id).with_for_update()
        ).scalar_one()
        if row.lease_owner != owner or row.lease_generation != generation:
            raise ValueError("租约已被回收，重试写入被 fencing 拒绝")
        was_ai = row.queue_state == QueueState.AI_RUNNING
        row.attempts += 1
        row.error = error
        row.lease_owner = None
        row.lease_until = EPOCH
        row.dispatch_phase = None
        row.updated_at = now
        if was_ai:
            self._release_slot(row.queue_key, now)
        if row.attempts >= row.max_attempts:
            row.queue_state = QueueState.FAILED
        else:
            row.queue_state = (
                QueueState.PENDING if row.task_type == TaskType.CASE_CLEAN else QueueState.READY
            )
            delay = min(30 * (2 ** (row.attempts - 1)), 1800)
            row.next_retry_at = now + timedelta(seconds=delay)
        self._session.flush()
        return QueueState(row.queue_state)

    def reclaim_confirmed(
        self, task_id: str, owner: str, generation: int, now: datetime
    ) -> bool:
        row = self._session.execute(
            select(TaskQueue).where(TaskQueue.task_id == task_id).with_for_update()
        ).scalar_one_or_none()
        if (
            row is None
            or row.lease_owner != owner
            or row.lease_generation != generation
            or row.queue_state != QueueState.AI_RUNNING
            or row.lease_until >= now
        ):
            return False
        row.queue_state = QueueState.READY
        row.dispatch_phase = None
        row.lease_owner = None
        row.lease_until = EPOCH
        row.updated_at = now
        self._release_slot(row.queue_key, now)
        self._session.flush()
        return True

    def _release_slot(self, queue_key: str, now: datetime) -> None:
        self._session.execute(
            update(UserQueue)
            .where(UserQueue.queue_key == queue_key)
            .values(in_flight=func.greatest(UserQueue.in_flight - 1, 0), updated_at=now)
        )
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/integration/test_queue_lease.py -v`

Expected: 4 passed.

- [ ] **Step 5: Commit checkpoint**

```bash
git add src/aitrace/scheduling src/aitrace/persistence/repositories tests/integration/test_queue_lease.py
git commit -m "feat: add DB queue lease, fencing, retry and reclaim"
```

---

### Task 8: Token-queue slot gate and atomic AI claim

**Files:**
- Create: `src/aitrace/scheduling/slot_gate.py`
- Modify: `src/aitrace/persistence/repositories/queue.py` (add `claim_ai`)
- Test: `tests/integration/test_slot_gate.py`

**Interfaces:**
- Produces: `acquire_slot(session, queue_key, now) -> bool`, `release_slot(session, queue_key, now) -> None`; `QueueRepository.claim_ai(owner, now) -> TaskQueueRow | None` which sets `AI_RUNNING` + `dispatch_phase=JIT_FETCH` and increments `in_flight` in one transaction.

- [ ] **Step 1: Write the failing slot tests**

```python
# tests/integration/test_slot_gate.py
from datetime import UTC, datetime, timedelta

from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from aitrace.core.enums import QueueState
from aitrace.persistence.repositories.queue import QueueRepository
from aitrace.scheduling.slot_gate import acquire_slot, release_slot

NOW = datetime(2026, 11, 25, 12, 0, tzinfo=UTC)


def test_slot_is_exclusive(db_session: Session) -> None:
    repo = QueueRepository(db_session)
    repo.ensure_user_queue("u1", NOW)
    db_session.commit()
    assert acquire_slot(db_session, "u1", NOW) is True
    assert acquire_slot(db_session, "u1", NOW) is False
    db_session.commit()
    release_slot(db_session, "u1", NOW)
    db_session.commit()
    assert acquire_slot(db_session, "u1", NOW) is True


def test_paused_queue_rejects_slot(db_session: Session) -> None:
    repo = QueueRepository(db_session)
    repo.ensure_user_queue("u1", NOW)
    db_session.execute(
        text("UPDATE user_queue SET state='PAUSED_AUTH', paused_until=:until WHERE queue_key='u1'"),
        {"until": NOW + timedelta(minutes=5)},
    )
    db_session.commit()
    assert acquire_slot(db_session, "u1", NOW) is False


def _seed_ready_task(db_session: Session, task_id: str, queue_key: str) -> None:
    db_session.execute(
        text(
            "INSERT INTO session (session_id, product_id, batch_id, case_id, domain_id, "
            "profile_version, trigger_user_id, hardware_type, latest_round_no, status, "
            "env_topology, git_context, created_at) VALUES "
            "(:sid,'p','b',:sid,'d','v',:u,'SSD',1,'RUNNING','{}','{}',now())"
        ),
        {"sid": f"S-{task_id}", "u": queue_key},
    )
    db_session.execute(
        text(
            "INSERT INTO task_queue (task_id, batch_id, session_id, round_id, round_no, "
            "product_id, case_id, task_type, queue_key, queue_state, priority, attempts, "
            "max_attempts, lease_generation, lease_until, callback_status, payload, "
            "created_at, updated_at) VALUES "
            "(:tid,'b',:sid,'R1',1,'p',:sid,'case_ai',:u,'READY',1,0,3,0,'epoch','PENDING',"
            "'{}',now(),now())"
        ),
        {"tid": task_id, "sid": f"S-{task_id}", "u": queue_key},
    )


def test_ai_claim_sets_running_and_slot_in_one_transaction(
    db_session: Session, db_engine: Engine
) -> None:
    repo = QueueRepository(db_session)
    repo.ensure_user_queue("u1", NOW)
    _seed_ready_task(db_session, "T1", "u1")
    db_session.commit()

    row = repo.claim_ai("worker-a", NOW)
    db_session.commit()
    assert row is not None and row.task_id == "T1"
    assert row.queue_state == QueueState.AI_RUNNING
    assert row.dispatch_phase == "JIT_FETCH"
    with db_engine.connect() as conn:
        assert conn.execute(text("SELECT in_flight FROM user_queue WHERE queue_key='u1'")).scalar_one() == 1
        assert conn.execute(text("SELECT queue_state FROM task_queue WHERE task_id='T1'")).scalar_one() == "AI_RUNNING"


def test_never_dispatched_queue_wins_fairness_round(db_session: Session) -> None:
    repo = QueueRepository(db_session)
    repo.ensure_user_queue("busy", NOW)
    repo.ensure_user_queue("fresh", NOW)
    db_session.execute(text("UPDATE user_queue SET last_dispatch_at=:t WHERE queue_key='busy'"), {"t": NOW})
    _seed_ready_task(db_session, "T-old", "busy")
    _seed_ready_task(db_session, "T-new", "fresh")
    db_session.commit()
    row = repo.claim_ai("worker-a", NOW)
    db_session.commit()
    assert row is not None
    assert row.task_id == "T-new"


def test_reclaim_confirmed_returns_ready_and_releases_slot(
    db_session: Session, db_engine: Engine
) -> None:
    repo = QueueRepository(db_session)
    repo.ensure_user_queue("u1", NOW)
    _seed_ready_task(db_session, "T1", "u1")
    db_session.commit()
    row = repo.claim_ai("worker-a", NOW)
    db_session.commit()
    assert row is not None
    db_session.execute(
        text("UPDATE task_queue SET lease_until=:past WHERE task_id='T1'"),
        {"past": NOW - timedelta(seconds=1)},
    )
    db_session.commit()
    assert repo.reclaim_confirmed("T1", "worker-a", row.lease_generation, NOW) is True
    db_session.commit()
    with db_engine.connect() as conn:
        assert conn.execute(text("SELECT queue_state FROM task_queue WHERE task_id='T1'")).scalar_one() == "READY"
        assert conn.execute(text("SELECT in_flight FROM user_queue WHERE queue_key='u1'")).scalar_one() == 0
```

- [ ] **Step 2: Run and confirm failure**

Run: `pytest tests/integration/test_slot_gate.py -v`

Expected: FAIL with `ModuleNotFoundError: No module named 'aitrace.scheduling.slot_gate'`.

- [ ] **Step 3: Implement the slot gate and AI claim**

```python
# src/aitrace/scheduling/slot_gate.py
from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, update
from sqlalchemy.orm import Session

from aitrace.persistence.models import UserQueue


def acquire_slot(session: Session, queue_key: str, now: datetime) -> bool:
    result = session.execute(
        update(UserQueue)
        .where(
            UserQueue.queue_key == queue_key,
            UserQueue.state == "ACTIVE",
            UserQueue.paused_until <= now,
            UserQueue.in_flight < 1,
        )
        .values(
            in_flight=UserQueue.in_flight + 1,
            state="ACTIVE",
            last_dispatch_at=now,
            updated_at=now,
        )
    )
    return result.rowcount == 1


def release_slot(session: Session, queue_key: str, now: datetime) -> None:
    session.execute(
        update(UserQueue)
        .where(UserQueue.queue_key == queue_key)
        .values(
            in_flight=func.greatest(UserQueue.in_flight - 1, 0),
            updated_at=now,
        )
    )
```

Add to `QueueRepository`:

```python
    def claim_ai(self, owner: str, now: datetime) -> TaskQueueRow | None:
        row = self._session.execute(
            select(TaskQueue)
            .join(UserQueue, UserQueue.queue_key == TaskQueue.queue_key)
            .where(
                TaskQueue.queue_state == QueueState.READY,
                TaskQueue.task_type.in_([TaskType.CASE_AI, TaskType.BATCH_COMMON]),
                TaskQueue.lease_until < now,
                or_(TaskQueue.not_before.is_(None), TaskQueue.not_before <= now),
                or_(TaskQueue.next_retry_at.is_(None), TaskQueue.next_retry_at <= now),
                UserQueue.state == "ACTIVE",
                UserQueue.paused_until <= now,
                UserQueue.in_flight < 1,
            )
            .order_by(
                func.coalesce(UserQueue.last_dispatch_at, EPOCH).asc(),
                TaskQueue.priority.asc(),
                TaskQueue.created_at.asc(),
            )
            .with_for_update(skip_locked=True, of=TaskQueue)
            .limit(1)
        ).scalar_one_or_none()
        if row is None:
            return None
        if not acquire_slot(self._session, row.queue_key, now):
            return None
        row.queue_state = QueueState.AI_RUNNING
        row.dispatch_phase = "JIT_FETCH"
        row.lease_owner = owner
        row.lease_generation += 1
        row.lease_until = now + self._lease
        row.updated_at = now
        self._session.flush()
        return row
```

Add `from aitrace.scheduling.slot_gate import acquire_slot` at the top of `queue.py`.

- [ ] **Step 4: Run tests**

Run: `pytest tests/integration/test_slot_gate.py -v`

Expected: 5 passed.

- [ ] **Step 5: Commit checkpoint**

```bash
git add src/aitrace/scheduling/slot_gate.py src/aitrace/persistence/repositories/queue.py tests/integration/test_slot_gate.py
git commit -m "feat: add token-queue slot gate and atomic AI claim"
```

---

### Task 9: Delivery ingestion and `POST /api/v1/aitrace/tasks`

**Files:**
- Create: `src/aitrace/api/schemas.py`
- Modify: `src/aitrace/api/deps.py` (add token auth and service getters)
- Create: `src/aitrace/api/routes/deliveries.py`
- Create: `src/aitrace/ingest/__init__.py`
- Create: `src/aitrace/ingest/service.py`
- Create: `src/aitrace/persistence/repositories/profiles.py`
- Modify: `src/aitrace/api/app.py` (load profiles, include router)
- Test: `tests/integration/test_delivery_api.py`

**Interfaces:**
- Produces: `DeliveryPayload` and nested models; `DeliveryResponse`, `SessionResult`; `IngestService(session, resolver, clock, ids).deliver(payload) -> IngestOutcome`; `POST /api/v1/aitrace/tasks` returning 202 when any session is new, 200 when all are idempotent hits; `require_service_token`.

- [ ] **Step 1: Write the failing delivery tests**

```python
# tests/integration/test_delivery_api.py
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.engine import Engine

from aitrace.api.app import create_app
from aitrace.core.settings import Settings

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
        "git_context": {"repo_url": "https://gitlab.internal/ssd-qa/nvme-auto.git", "commit_id": "a9c4e12f"},
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


def test_delivery_creates_session_round_queue_and_batch(
    test_settings: Settings, db_engine: Engine
) -> None:
    with TestClient(create_app(test_settings)) as client:
        response = client.post("/api/v1/aitrace/tasks", json=_payload(), headers=HEADERS)
    assert response.status_code == 202
    body = response.json()
    assert body["accepted"] is True
    assert body["batch_task_id"] == "T-BATCH-20261120-0089"
    assert body["sessions"][0]["case_id"] == "CASE-1021"
    assert body["sessions"][0]["status"] == "ACCEPTED"
    assert body["rejected"] == []

    with db_engine.connect() as conn:
        session = conn.execute(
            text("SELECT domain_id, profile_version, trigger_user_id FROM session WHERE case_id='CASE-1021'")
        ).one()
        assert session[0] == "enterprise"
        assert len(session[1]) == 16
        assert session[2] == "zhangsan"
        assert conn.execute(text("SELECT COUNT(*) FROM \"round\"")).scalar_one() == 1
        task = conn.execute(
            text("SELECT queue_state, task_type, queue_key FROM task_queue")
        ).one()
        assert task == ("PENDING", "case_clean", "zhangsan")
        assert conn.execute(text("SELECT in_flight FROM user_queue WHERE queue_key='zhangsan'")).scalar_one() == 0
        assert conn.execute(text("SELECT batch_state FROM batch")).scalar_one() == "RUNNING"


def test_duplicate_delivery_returns_existing_without_new_round(
    test_settings: Settings, db_engine: Engine
) -> None:
    app = create_app(test_settings)
    with TestClient(app) as client:
        first = client.post("/api/v1/aitrace/tasks", json=_payload(), headers=HEADERS)
        second = client.post("/api/v1/aitrace/tasks", json=_payload(), headers=HEADERS)
    assert first.status_code == 202
    assert second.status_code == 200
    assert second.json()["sessions"][0]["is_existing"] is True
    assert second.json()["sessions"][0]["status"] == "EXISTING"
    with db_engine.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM \"round\"")).scalar_one() == 1
        assert conn.execute(text("SELECT COUNT(*) FROM task_queue")).scalar_one() == 1


def test_missing_trigger_user_reports_chinese_field(
    test_settings: Settings,
) -> None:
    payload = _payload()
    del payload["trigger_user_id"]
    with TestClient(create_app(test_settings)) as client:
        response = client.post("/api/v1/aitrace/tasks", json=payload, headers=HEADERS)
    assert response.status_code == 400
    body = response.json()
    assert body["code"] == "INVALID_PAYLOAD"
    assert "触发人" in body["message"]


def test_invalid_hardware_type_lists_allowed_values(test_settings: Settings) -> None:
    with TestClient(create_app(test_settings)) as client:
        response = client.post(
            "/api/v1/aitrace/tasks", json=_payload(hardware_type="HDD"), headers=HEADERS
        )
    assert response.status_code == 400
    assert "合法取值" in response.json()["message"]
    assert "SSD" in response.json()["message"]


def test_unknown_device_binding_is_rejected(test_settings: Settings) -> None:
    payload = _payload()
    payload["failed_cases"][0]["env_binding"] = {"device_id": "D9"}  # type: ignore[index]
    with TestClient(create_app(test_settings)) as client:
        response = client.post("/api/v1/aitrace/tasks", json=payload, headers=HEADERS)
    assert response.status_code == 400
    assert "设备清单" in response.json()["message"]


def test_payload_with_qoder_token_is_rejected(test_settings: Settings) -> None:
    with TestClient(create_app(test_settings)) as client:
        response = client.post(
            "/api/v1/aitrace/tasks",
            json=_payload(qoder_access_token="forbidden"),
            headers=HEADERS,
        )
    assert response.status_code == 400
    assert response.json()["code"] == "INVALID_PAYLOAD"


def test_invalid_service_token_is_forbidden(test_settings: Settings) -> None:
    with TestClient(create_app(test_settings)) as client:
        response = client.post(
            "/api/v1/aitrace/tasks",
            json=_payload(),
            headers={"Authorization": "Bearer wrong"},
        )
    assert response.status_code == 403
    assert response.json()["code"] == "FORBIDDEN"
```

- [ ] **Step 2: Run and confirm failure**

Run: `pytest tests/integration/test_delivery_api.py -v`

Expected: FAIL with 404 because the route does not exist.

- [ ] **Step 3: Implement DTOs**

```python
# src/aitrace/api/schemas.py
from __future__ import annotations

import ipaddress
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DeviceInfo(_StrictModel):
    device_id: str = Field(min_length=1)
    pcie_bdf: str = Field(min_length=1)
    device_node: str = Field(min_length=1)
    model: str = Field(min_length=1)
    sn: str = Field(min_length=1)
    fw_version: str = Field(min_length=1)
    slot: str = Field(min_length=1)


class EnvTopology(_StrictModel):
    host_ip: str
    host_boot_time_utc: int = Field(gt=0)
    devices: list[DeviceInfo] = Field(min_length=1)

    @field_validator("host_ip")
    @classmethod
    def _valid_ipv4(cls, value: str) -> str:
        ipaddress.IPv4Address(value)
        return value


class GitContext(_StrictModel):
    repo_url: str = Field(min_length=1)
    commit_id: str = Field(min_length=1)


class LogPaths(_StrictModel):
    test_log_path: str | None = None
    uart_log_path: str | None = None
    kernel_log_path: str | None = None
    scheduler_log_path: str | None = None


class FailedCase(_StrictModel):
    case_id: str = Field(min_length=1)
    script_path: str = Field(min_length=1)
    failed_at_utc: int = Field(gt=0)
    env_binding: dict[str, str]
    logs: LogPaths


class PassedCase(_StrictModel):
    case_id: str = Field(min_length=1)
    script_path: str = Field(min_length=1)


class DeliveryPayload(_StrictModel):
    product_id: str = Field(min_length=1)
    batch_id: str = Field(min_length=1)
    trigger_user_id: str = Field(min_length=1)
    hardware_type: Literal["SSD", "CXL"]
    env_topology: EnvTopology
    git_context: GitContext
    failed_cases: list[FailedCase] = Field(min_length=1)
    passed_cases: list[PassedCase] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_cases(self) -> Self:
        device_ids = {device.device_id for device in self.env_topology.devices}
        seen: set[str] = set()
        for case in self.failed_cases:
            if case.case_id in seen:
                raise ValueError(f"失败用例重复：{case.case_id}")
            seen.add(case.case_id)
            if case.env_binding.get("device_id") not in device_ids:
                raise ValueError(
                    f"用例 {case.case_id} 的 env_binding.device_id 不在设备清单中"
                )
            if not any(case.logs.model_dump().values()):
                raise ValueError(f"用例 {case.case_id} 未提供任何日志路径")
        return self


class SessionResult(_StrictModel):
    case_id: str
    session_id: str
    is_existing: bool
    status: Literal["ACCEPTED", "EXISTING"]


class RejectedCase(_StrictModel):
    case_id: str
    code: str
    message: str
    retryable: bool
    retry_after: int | None = None


class DeliveryResponse(_StrictModel):
    accepted: bool
    batch_task_id: str
    sessions: list[SessionResult]
    rejected: list[RejectedCase] = Field(default_factory=list)
```

- [ ] **Step 4: Implement profile snapshot, ingest service, and route**

```python
# src/aitrace/persistence/repositories/profiles.py
from __future__ import annotations

from datetime import datetime

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from aitrace.persistence.models import Profile
from aitrace.profiles.resolver import ResolvedProfile


class ProfileSnapshotRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def upsert(self, resolved: ResolvedProfile, now: datetime) -> None:
        self._session.execute(
            insert(Profile)
            .values(
                domain_id=resolved.domain_id,
                config=resolved.config.model_dump(mode="json"),
                version=resolved.version,
                updated_by="system",
                updated_at=now,
            )
            .on_conflict_do_update(
                index_elements=["domain_id"],
                set_={
                    "config": resolved.config.model_dump(mode="json"),
                    "version": resolved.version,
                    "updated_by": "system",
                    "updated_at": now,
                },
            )
        )
```

```python
# src/aitrace/ingest/service.py
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from aitrace.api.schemas import DeliveryPayload, DeliveryResponse, SessionResult
from aitrace.core.enums import Stage, TaskType
from aitrace.core.ids import SystemClock, UuidIdGenerator
from aitrace.persistence.models import Batch, Round, SessionModel
from aitrace.persistence.repositories.profiles import ProfileSnapshotRepository
from aitrace.persistence.repositories.queue import QueueRepository
from aitrace.profiles.resolver import ProfileResolver
from aitrace.scheduling.types import NewTask


@dataclass(frozen=True)
class IngestOutcome:
    response: DeliveryResponse
    created_any: bool


class IngestService:
    def __init__(
        self,
        session: Session,
        resolver: ProfileResolver,
        clock: SystemClock,
        ids: UuidIdGenerator,
    ) -> None:
        self._session = session
        self._resolver = resolver
        self._clock = clock
        self._ids = ids

    def deliver(self, payload: DeliveryPayload) -> IngestOutcome:
        now = self._clock.now()
        profile = self._resolver.resolve(payload.product_id)
        batch_task_id = self._upsert_batch(payload, now)
        ProfileSnapshotRepository(self._session).upsert(profile, now)
        queue = QueueRepository(self._session)

        results: list[SessionResult] = []
        created_any = False
        for case in payload.failed_cases:
            session_id = self._ids.new("S")
            inserted = self._session.execute(
                insert(SessionModel)
                .values(
                    session_id=session_id,
                    product_id=payload.product_id,
                    batch_id=payload.batch_id,
                    case_id=case.case_id,
                    domain_id=profile.domain_id,
                    profile_version=profile.version,
                    trigger_user_id=payload.trigger_user_id,
                    hardware_type=payload.hardware_type,
                    stable_fingerprint=None,
                    env_topology=payload.env_topology.model_dump(mode="json"),
                    git_context=payload.git_context.model_dump(mode="json"),
                    latest_round_no=1,
                    status="RUNNING",
                    citation_group_id=None,
                    created_at=now,
                )
                .on_conflict_do_nothing(index_elements=["product_id", "batch_id", "case_id"])
                .returning(SessionModel.session_id)
            ).scalar_one_or_none()

            if inserted is None:
                existing = self._session.execute(
                    select(SessionModel.session_id).where(
                        SessionModel.product_id == payload.product_id,
                        SessionModel.batch_id == payload.batch_id,
                        SessionModel.case_id == case.case_id,
                    )
                ).scalar_one()
                results.append(
                    SessionResult(
                        case_id=case.case_id,
                        session_id=existing,
                        is_existing=True,
                        status="EXISTING",
                    )
                )
                continue

            created_any = True
            round_id = self._ids.new("R")
            self._session.add(
                Round(
                    round_id=round_id,
                    session_id=session_id,
                    round_no=1,
                    trigger_user_id=payload.trigger_user_id,
                    queue_key=payload.trigger_user_id,
                    exact_fingerprint=None,
                    stage=Stage.QUEUED,
                    round_status="RUNNING",
                    report_id=None,
                    confidence=None,
                    feedback=None,
                    started_at=now,
                    finished_at=None,
                )
            )
            queue.enqueue(
                NewTask(
                    task_id=self._ids.new("T"),
                    batch_id=payload.batch_id,
                    session_id=session_id,
                    round_id=round_id,
                    round_no=1,
                    product_id=payload.product_id,
                    case_id=case.case_id,
                    task_type=TaskType.CASE_CLEAN,
                    queue_key=payload.trigger_user_id,
                    payload={
                        "script_path": case.script_path,
                        "failed_at_utc": case.failed_at_utc,
                        "env_binding": case.env_binding,
                        "logs": case.logs.model_dump(mode="json"),
                    },
                ),
                now,
            )
            results.append(
                SessionResult(
                    case_id=case.case_id,
                    session_id=session_id,
                    is_existing=False,
                    status="ACCEPTED",
                )
            )

        self._session.flush()
        return IngestOutcome(
            response=DeliveryResponse(
                accepted=True,
                batch_task_id=batch_task_id,
                sessions=results,
                rejected=[],
            ),
            created_any=created_any,
        )

    def _upsert_batch(self, payload: DeliveryPayload, now: datetime) -> str:
        suffix = payload.batch_id.removeprefix("BATCH-")
        batch_task_id = f"T-BATCH-{suffix}"
        failed = [case.model_dump(mode="json") for case in payload.failed_cases]
        passed = [case.model_dump(mode="json") for case in payload.passed_cases]
        self._session.execute(
            insert(Batch)
            .values(
                batch_id=payload.batch_id,
                product_id=payload.product_id,
                trigger_user_id=payload.trigger_user_id,
                batch_task_id=batch_task_id,
                failed_cases=failed,
                passed_cases=passed,
                batch_state="RUNNING",
                updated_at=now,
            )
            .on_conflict_do_update(
                index_elements=["batch_id"],
                set_={
                    "failed_cases": failed,
                    "passed_cases": passed,
                    "trigger_user_id": payload.trigger_user_id,
                    "updated_at": now,
                },
            )
        )
        return batch_task_id
```

```python
# src/aitrace/api/deps.py (extend)
from __future__ import annotations

from collections.abc import Iterator

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from aitrace.core.errors import AitraceError
from aitrace.core.ids import SystemClock, UuidIdGenerator
from aitrace.core.settings import Settings
from aitrace.profiles.resolver import ProfileResolver

_bearer = HTTPBearer(auto_error=False)


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_session(request: Request) -> Iterator[Session]:
    with request.app.state.session_factory.begin() as session:
        yield session


def get_resolver(request: Request) -> ProfileResolver:
    return request.app.state.profile_resolver


def get_clock(request: Request) -> SystemClock:
    return request.app.state.clock


def get_ids(request: Request) -> UuidIdGenerator:
    return request.app.state.ids


def require_service_token(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    settings: Settings = Depends(get_settings),
) -> None:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise AitraceError("FORBIDDEN", "服务令牌缺失或格式非法", http_status=403)
    expected = settings.service_token.get_secret_value() if settings.service_token else ""
    if not expected or credentials.credentials != expected:
        raise AitraceError("FORBIDDEN", "服务令牌无效", http_status=403)
```

```python
# src/aitrace/api/routes/deliveries.py
from fastapi import APIRouter, Depends, Response
from sqlalchemy.orm import Session

from aitrace.api.deps import (
    get_clock,
    get_ids,
    get_resolver,
    get_session,
    require_service_token,
)
from aitrace.api.schemas import DeliveryPayload, DeliveryResponse
from aitrace.core.ids import SystemClock, UuidIdGenerator
from aitrace.ingest.service import IngestService
from aitrace.profiles.resolver import ProfileResolver

router = APIRouter(tags=["delivery"])


@router.post(
    "/aitrace/tasks",
    response_model=DeliveryResponse,
    dependencies=[Depends(require_service_token)],
)
def create_tasks(
    payload: DeliveryPayload,
    response: Response,
    session: Session = Depends(get_session),
    resolver: ProfileResolver = Depends(get_resolver),
    clock: SystemClock = Depends(get_clock),
    ids: UuidIdGenerator = Depends(get_ids),
) -> DeliveryResponse:
    outcome = IngestService(session, resolver, clock, ids).deliver(payload)
    response.status_code = 202 if outcome.created_any else 200
    return outcome.response
```

Update `api/app.py` to load profiles and register the delivery router:

```python
from aitrace.api.routes import deliveries, health
from aitrace.profiles.loader import load_profiles
from aitrace.profiles.resolver import ProfileResolver

# inside create_app, after settings is resolved and before FastAPI(...)
registry = load_profiles(settings.storage.profile_domains_dir)

# inside the app setup block
app.state.profile_resolver = ProfileResolver(registry)

# after install_error_handlers(app)
app.include_router(health.router, prefix="/api/v1")
app.include_router(deliveries.router, prefix="/api/v1")
```

- [ ] **Step 5: Run tests**

Run: `pytest tests/integration/test_delivery_api.py -v`

Expected: 7 passed.

- [ ] **Step 6: Commit checkpoint**

```bash
git add src/aitrace/api src/aitrace/ingest src/aitrace/persistence/repositories/profiles.py tests/integration/test_delivery_api.py
git commit -m "feat: accept failed-case deliveries idempotently"
```

---

### Task 10: Session status query and stage mapping

**Files:**
- Create: `src/aitrace/scheduling/stage_view.py`
- Create: `src/aitrace/api/routes/sessions.py`
- Modify: `src/aitrace/api/app.py` (include sessions router)
- Test: `tests/unit/test_stage_view.py`
- Test: `tests/integration/test_session_query.py`

**Interfaces:**
- Produces: `StageView(stage, progress, waiting_reason, message)`; `build_stage_view(task, queue, now) -> StageView`; `GET /api/v1/aitrace/tasks/{session_id}`.

- [ ] **Step 1: Write the failing stage tests**

```python
# tests/unit/test_stage_view.py
from datetime import UTC, datetime, timedelta

import pytest

from aitrace.scheduling.stage_view import build_stage_view

NOW = datetime(2026, 11, 25, 12, 0, tzinfo=UTC)


class Task:
    def __init__(self, **kwargs: object) -> None:
        self.queue_state = "PENDING"
        self.dispatch_phase = None
        self.next_retry_at = None
        self.depends_on_task_id = None
        self.not_before = None
        for key, value in kwargs.items():
            setattr(self, key, value)


class Queue:
    def __init__(self, state: str = "ACTIVE") -> None:
        self.state = state


@pytest.mark.parametrize(
    ("task", "queue", "stage", "progress", "reason"),
    [
        (Task(queue_state="PENDING"), Queue(), "QUEUED", 10, None),
        (Task(queue_state="CLEANING"), Queue(), "CLEANING", 30, None),
        (Task(queue_state="READY"), Queue(), "QUEUED", 10, "token_busy"),
        (Task(queue_state="READY"), Queue("PAUSED_AUTH"), "QUEUED", 10, "paused_auth"),
        (Task(queue_state="AI_RUNNING", dispatch_phase="JIT_FETCH"), Queue(), "QUEUED", 10, "token_busy"),
        (Task(queue_state="AI_RUNNING", dispatch_phase="AI_EXEC"), Queue(), "AI_CLASSIFYING", 50, None),
        (Task(queue_state="DONE"), Queue(), "COMPLETED", 100, None),
        (Task(queue_state="FAILED"), Queue(), "FAILED", None, None),
    ],
)
def test_stage_mapping(
    task: Task, queue: Queue, stage: str, progress: int | None, reason: str | None
) -> None:
    view = build_stage_view(task, queue, NOW)  # type: ignore[arg-type]
    assert view.stage == stage
    assert view.progress == progress
    assert view.waiting_reason == reason


def test_retry_backoff_takes_precedence_for_pending() -> None:
    task = Task(queue_state="PENDING", next_retry_at=NOW + timedelta(seconds=30))
    view = build_stage_view(task, Queue(), NOW)  # type: ignore[arg-type]
    assert view.waiting_reason == "retry_backoff"


def test_waiting_dependency_maps_to_citation_pending() -> None:
    task = Task(queue_state="WAITING", depends_on_task_id="T0")
    view = build_stage_view(task, Queue(), NOW)  # type: ignore[arg-type]
    assert view.stage == "QUEUED"
    assert view.waiting_reason == "citation_pending"


def test_not_before_maps_to_not_before() -> None:
    task = Task(queue_state="WAITING", not_before=NOW + timedelta(minutes=5))
    view = build_stage_view(task, Queue(), NOW)  # type: ignore[arg-type]
    assert view.waiting_reason == "not_before"
```

- [ ] **Step 2: Run and confirm failure**

Run: `pytest tests/unit/test_stage_view.py -v`

Expected: FAIL with `ModuleNotFoundError: No module named 'aitrace.scheduling.stage_view'`.

- [ ] **Step 3: Implement stage mapping**

```python
# src/aitrace/scheduling/stage_view.py
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from aitrace.core.enums import QueueState, Stage, WaitingReason

_MESSAGES = {
    Stage.QUEUED: "排队等待分析中...",
    Stage.CLEANING: "正在进行多源日志脱水、首错定位、三段切片与时间对齐...",
    Stage.AI_CLASSIFYING: "Qoder 主引擎正在全局归类并定界...",
    Stage.DEEP_PROBING: "正在检索源码并实查 SSD/CXL 状态...",
    Stage.COMPLETED: "知因分析完成（点击查看根因报告）",
    Stage.FAILED: "分析异常（支持一键重新触发）",
}


@dataclass(frozen=True)
class StageView:
    stage: str
    progress: int | None
    waiting_reason: str | None
    message: str


def build_stage_view(task: Any, queue: Any, now: datetime) -> StageView:
    state = QueueState(task.queue_state)
    if state is QueueState.FAILED:
        return StageView(Stage.FAILED, None, None, _MESSAGES[Stage.FAILED])
    if state is QueueState.DONE:
        return StageView(Stage.COMPLETED, 100, None, _MESSAGES[Stage.COMPLETED])
    if state is QueueState.CLEANING:
        return StageView(Stage.CLEANING, 30, None, _MESSAGES[Stage.CLEANING])
    if state is QueueState.AI_RUNNING and task.dispatch_phase == "AI_EXEC":
        return StageView(Stage.AI_CLASSIFYING, 50, None, _MESSAGES[Stage.AI_CLASSIFYING])

    reason = _waiting_reason(task, queue, now)
    return StageView(Stage.QUEUED, 10, reason, _MESSAGES[Stage.QUEUED])


def _waiting_reason(task: Any, queue: Any, now: datetime) -> str | None:
    if queue.state == "PAUSED_AUTH":
        return WaitingReason.PAUSED_AUTH
    if task.next_retry_at is not None and task.next_retry_at > now:
        return WaitingReason.RETRY_BACKOFF
    state = QueueState(task.queue_state)
    if state is QueueState.WAITING:
        if task.not_before is not None and task.not_before > now:
            return WaitingReason.NOT_BEFORE
        return WaitingReason.CITATION_PENDING
    if state is QueueState.AI_RUNNING:
        return WaitingReason.TOKEN_BUSY
    if state is QueueState.READY:
        return WaitingReason.TOKEN_BUSY
    return None
```

- [ ] **Step 4: Implement the query route**

Add to `src/aitrace/api/schemas.py`:

```python
class SessionStatusResponse(_StrictModel):
    session_id: str
    batch_id: str
    case_id: str
    product_id: str
    hardware_type: str
    domain_id: str
    profile_version: str
    round_no: int
    stage: str
    progress: int | None
    waiting_reason: str | None
    message: str
    report: dict[str, Any] | None = None
    citation: dict[str, Any] | None = None
```

Add `from typing import Any` import. Then:

```python
# src/aitrace/api/routes/sessions.py
from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from aitrace.api.deps import get_clock, get_session, require_service_token
from aitrace.api.schemas import SessionStatusResponse
from aitrace.core.errors import AitraceError
from aitrace.core.ids import SystemClock
from aitrace.persistence.models import Report, Round, SessionModel, TaskQueue, UserQueue
from aitrace.scheduling.stage_view import build_stage_view

router = APIRouter(tags=["sessions"])


@router.get(
    "/aitrace/tasks/{session_id}",
    response_model=SessionStatusResponse,
    dependencies=[Depends(require_service_token)],
)
def get_session_status(
    session_id: str,
    session: Session = Depends(get_session),
    clock: SystemClock = Depends(get_clock),
) -> SessionStatusResponse:
    record = session.get(SessionModel, session_id)
    if record is None:
        raise AitraceError("DELIVERY_NOT_FOUND", "会话不存在", http_status=404)
    round_row = session.execute(
        select(Round).where(Round.session_id == session_id).order_by(Round.round_no.desc()).limit(1)
    ).scalar_one()
    task = session.execute(
        select(TaskQueue)
        .where(TaskQueue.session_id == session_id, TaskQueue.task_type == "case_clean")
        .order_by(TaskQueue.created_at.desc())
        .limit(1)
    ).scalar_one_or_none()
    queue = session.get(UserQueue, record.trigger_user_id)
    if task is None or queue is None:
        raise AitraceError("DELIVERY_NOT_FOUND", "会话缺少排队任务", http_status=404)
    view = build_stage_view(task, queue, clock.now())
    report = session.execute(
        select(Report).where(Report.round_id == round_row.round_id)
    ).scalar_one_or_none()
    return SessionStatusResponse(
        session_id=record.session_id,
        batch_id=record.batch_id,
        case_id=record.case_id,
        product_id=record.product_id,
        hardware_type=record.hardware_type,
        domain_id=record.domain_id,
        profile_version=record.profile_version,
        round_no=round_row.round_no,
        stage=view.stage,
        progress=view.progress,
        waiting_reason=view.waiting_reason,
        message=view.message,
        report=None if report is None else {"report_id": report.report_id, "result": report.result},
        citation=None if report is None else report.citation,
    )
```

Register in `api/app.py`:

```python
from aitrace.api.routes import deliveries, health, sessions
...
    app.include_router(health.router, prefix="/api/v1")
    app.include_router(deliveries.router, prefix="/api/v1")
    app.include_router(sessions.router, prefix="/api/v1")
```

- [ ] **Step 5: Write the query integration test**

```python
# tests/integration/test_session_query.py
from fastapi.testclient import TestClient

from aitrace.api.app import create_app
from aitrace.core.settings import Settings
from tests.integration.test_delivery_api import HEADERS, _payload


def test_query_returns_queued_stage(test_settings: Settings) -> None:
    with TestClient(create_app(test_settings)) as client:
        created = client.post("/api/v1/aitrace/tasks", json=_payload(), headers=HEADERS).json()
        session_id = created["sessions"][0]["session_id"]
        response = client.get(f"/api/v1/aitrace/tasks/{session_id}", headers=HEADERS)
    assert response.status_code == 200
    body = response.json()
    assert body["session_id"] == session_id
    assert body["stage"] == "QUEUED"
    assert body["progress"] == 10
    assert body["round_no"] == 1


def test_query_unknown_session_returns_404(test_settings: Settings) -> None:
    with TestClient(create_app(test_settings)) as client:
        response = client.get("/api/v1/aitrace/tasks/S-missing", headers=HEADERS)
    assert response.status_code == 404
    assert response.json()["code"] == "DELIVERY_NOT_FOUND"
```

- [ ] **Step 6: Run tests**

Run: `pytest tests/unit/test_stage_view.py tests/integration/test_session_query.py -v`

Expected: 13 passed.

- [ ] **Step 7: Commit checkpoint**

```bash
git add src/aitrace/scheduling/stage_view.py src/aitrace/api/routes/sessions.py src/aitrace/api/app.py src/aitrace/api/schemas.py tests/unit/test_stage_view.py tests/integration/test_session_query.py
git commit -m "feat: expose session status with two-level stage mapping"
```

---

### Task 11: Contract export, drift check, and token scan

**Files:**
- Create: `tools/export_contracts.py`
- Create: `contracts/aitrace-inbound.openapi.yaml` (generated)
- Create: `contracts/schemas/delivery.schema.json` (generated)
- Create: `contracts/schemas/session-status.schema.json` (generated)
- Create: `contracts/schemas/error.schema.json` (generated)
- Test: `tests/contract/test_openapi_contract.py`

**Interfaces:**
- Produces: deterministic contract files; `python tools/export_contracts.py --check` exits non-zero on drift; forbidden token field scan.

- [ ] **Step 1: Write the failing contract test**

```python
# tests/contract/test_openapi_contract.py
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_contract_files_are_current() -> None:
    result = subprocess.run(
        [sys.executable, "tools/export_contracts.py", "--check"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr + result.stdout


def test_paths_are_frozen() -> None:
    openapi_text = (ROOT / "contracts/aitrace-inbound.openapi.yaml").read_text(encoding="utf-8")
    assert "/api/v1/aitrace/tasks:" in openapi_text
    assert "/api/v1/aitrace/tasks/{session_id}:" in openapi_text
    assert "/api/v1/health:" in openapi_text


def _keys(node: object) -> set[str]:
    found: set[str] = set()
    if isinstance(node, dict):
        for key, value in node.items():
            found.add(str(key).lower())
            found |= _keys(value)
    elif isinstance(node, list):
        for value in node:
            found |= _keys(value)
    return found


def test_no_token_body_fields() -> None:
    forbidden = {"qoder_access_token", "access_token", "token_value", "token_body"}
    for path in (ROOT / "contracts/schemas").glob("*.json"):
        assert not (forbidden & _keys(json.loads(path.read_text(encoding="utf-8")))), path.name
    openapi = (ROOT / "contracts/aitrace-inbound.openapi.yaml").read_text(encoding="utf-8")
    for name in forbidden:
        assert name not in openapi
```

- [ ] **Step 2: Run and confirm failure**

Run: `pytest tests/contract/test_openapi_contract.py -v`

Expected: FAIL because `tools/export_contracts.py` does not exist.

- [ ] **Step 3: Implement the exporter**

```python
# tools/export_contracts.py
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml
from pydantic import SecretStr

from aitrace.api.app import create_app
from aitrace.api.schemas import DeliveryPayload, DeliveryResponse, SessionStatusResponse
from aitrace.core.settings import DatabaseSettings, Settings

ROOT = Path(__file__).resolve().parents[1]
CONTRACTS = ROOT / "contracts"


def _app():
    settings = Settings(
        service_token=SecretStr("contract-export"),
        database=DatabaseSettings(dsn="postgresql+psycopg://unused:unused@127.0.0.1:1/unused"),
    )
    return create_app(settings)


def _schema(model: type) -> str:
    return json.dumps(model.model_json_schema(), ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def _error_schema() -> str:
    return json.dumps(
        {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "title": "AitraceError",
            "type": "object",
            "required": ["code", "message", "retryable"],
            "properties": {
                "code": {"type": "string"},
                "message": {"type": "string"},
                "retryable": {"type": "boolean"},
                "retry_after": {"type": ["integer", "null"]},
                "details": {"type": "array", "items": {"type": "object"}},
            },
            "additionalProperties": False,
        },
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    ) + "\n"


def render() -> dict[Path, str]:
    openapi = _app().openapi()
    return {
        CONTRACTS / "aitrace-inbound.openapi.yaml": yaml.safe_dump(
            openapi, allow_unicode=True, sort_keys=False
        ),
        CONTRACTS / "schemas/delivery.schema.json": _schema(DeliveryPayload),
        CONTRACTS / "schemas/delivery-response.schema.json": _schema(DeliveryResponse),
        CONTRACTS / "schemas/session-status.schema.json": _schema(SessionStatusResponse),
        CONTRACTS / "schemas/error.schema.json": _error_schema(),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    rendered = render()
    if args.check:
        drift = []
        for path, content in rendered.items():
            if not path.is_file() or path.read_text(encoding="utf-8") != content:
                drift.append(str(path.relative_to(ROOT)))
        if drift:
            print("契约文件与代码不一致，请运行 python tools/export_contracts.py：" + ", ".join(drift))
            return 1
        return 0
    for path, content in rendered.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    print(f"已生成 {len(rendered)} 个契约文件")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Generate contracts and run tests**

Run: `python tools/export_contracts.py && pytest tests/contract/test_openapi_contract.py -v`

Expected: contract files created; 3 tests pass.

- [ ] **Step 5: Commit checkpoint**

```bash
git add tools/export_contracts.py contracts tests/contract/test_openapi_contract.py
git commit -m "feat: export inbound contracts with drift and token checks"
```

---

### Task 12: Isolated aitest mock

**Files:**
- Create: `mocks/aitest/pyproject.toml`
- Create: `mocks/aitest/src/aitest_mock/__init__.py`
- Create: `mocks/aitest/src/aitest_mock/main.py`
- Create: `mocks/aitest/scenarios/default.yaml`
- Test: `tests/contract/test_aitest_mock.py`

**Interfaces:**
- Produces: `GET /api/v1/aitest/token`, `GET /api/v1/aitest/cases?batch_id=`, `POST /api/v1/aitest/aitrace/status`, `GET /__mock/callbacks`, `POST /__mock/reset`, `GET /api/v1/health`; requires `X-Aitest-Read-Token` except health; scenario header `X-Mock-Scenario` with `token_not_found` and `token_expired`.

- [ ] **Step 1: Write the failing mock tests**

```python
# tests/contract/test_aitest_mock.py
from fastapi.testclient import TestClient

from aitest_mock.main import app

READ = {"X-Aitest-Read-Token": "mock-read-token"}


def test_token_endpoint_returns_token_and_expiry() -> None:
    with TestClient(app) as client:
        response = client.get(
            "/api/v1/aitest/token",
            params={
                "product_id": "ssd_9560",
                "batch_id": "B1",
                "case_id": "C1",
                "trigger_user_id": "zhangsan",
                "action": "auto",
            },
            headers=READ,
        )
    assert response.status_code == 200
    body = response.json()
    assert body["token"].startswith("mock-token-")
    assert body["expires_at"] > 0


def test_token_scenarios_return_contract_errors() -> None:
    with TestClient(app) as client:
        missing = client.get(
            "/api/v1/aitest/token",
            params={"product_id": "p", "batch_id": "b", "case_id": "c", "trigger_user_id": "u", "action": "auto"},
            headers={**READ, "X-Mock-Scenario": "token_not_found"},
        )
        expired = client.get(
            "/api/v1/aitest/token",
            params={"product_id": "p", "batch_id": "b", "case_id": "c", "trigger_user_id": "u", "action": "auto"},
            headers={**READ, "X-Mock-Scenario": "token_expired"},
        )
    assert missing.status_code == 404
    assert missing.json()["code"] == "TOKEN_NOT_FOUND"
    assert expired.status_code == 410
    assert expired.json()["code"] == "TOKEN_EXPIRED"


def test_callback_receiver_records_progress() -> None:
    with TestClient(app) as client:
        client.post("/__mock/reset", headers=READ)
        response = client.post(
            "/api/v1/aitest/aitrace/status",
            json={"event": "PROGRESS", "session_id": "S1", "round_no": 1, "seq": 1},
            headers=READ,
        )
        callbacks = client.get("/__mock/callbacks", headers=READ).json()
    assert response.status_code == 200
    assert callbacks["items"][0]["session_id"] == "S1"


def test_read_token_is_required() -> None:
    with TestClient(app) as client:
        response = client.get("/api/v1/aitest/cases", params={"batch_id": "B1"})
    assert response.status_code == 403
```

- [ ] **Step 2: Run and confirm failure**

Run: `pip install -e mocks/aitest && pytest tests/contract/test_aitest_mock.py -v`

Expected: FAIL because the package does not exist.

- [ ] **Step 3: Implement the mock**

```toml
# mocks/aitest/pyproject.toml
[build-system]
requires = ["hatchling>=1.25"]
build-backend = "hatchling.build"

[project]
name = "aitest-mock"
version = "0.1.0"
requires-python = ">=3.13"
dependencies = [
  "fastapi>=0.115,<1",
  "uvicorn[standard]>=0.32,<1",
  "pyyaml>=6.0,<7",
]

[tool.hatch.build.targets.wheel]
packages = ["src/aitest_mock"]
```

```yaml
# mocks/aitest/scenarios/default.yaml
batch_id: BATCH-20261120-0089
cases:
  - case_id: CASE-1021
    status: FAILED
  - case_id: CASE-0987
    status: PASSED
```

```python
# mocks/aitest/src/aitest_mock/main.py
from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Annotated, Any

import yaml
from fastapi import FastAPI, Header, Query, Request
from fastapi.responses import JSONResponse

app = FastAPI(title="aitest mock", version="0.1.0")

_READ_TOKEN = os.getenv("AITEST_READ_TOKEN", "mock-read-token")
_SCENARIO = Path(__file__).resolve().parents[2] / "scenarios/default.yaml"
_callbacks: list[dict[str, Any]] = []


class MockApiError(Exception):
    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


@app.exception_handler(MockApiError)
async def _mock_error(_: Request, exc: MockApiError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={"code": exc.code, "message": exc.message, "retryable": False},
    )


def _auth(token: str | None) -> None:
    if token != _READ_TOKEN:
        raise MockApiError(403, "FORBIDDEN", "读取令牌无效")


@app.get("/api/v1/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/v1/aitest/token")
def token(
    product_id: str,
    batch_id: str,
    case_id: str,
    trigger_user_id: str,
    action: str,
    x_aitest_read_token: Annotated[str | None, Header()] = None,
    x_mock_scenario: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    _auth(x_aitest_read_token)
    if x_mock_scenario == "token_not_found":
        raise MockApiError(404, "TOKEN_NOT_FOUND", "用户未托管令牌")
    if x_mock_scenario == "token_expired":
        raise MockApiError(410, "TOKEN_EXPIRED", "令牌已过期")
    return {
        "token": f"mock-token-{trigger_user_id}",
        "expires_at": int(time.time()) + 3600,
        "binding": {"product_id": product_id, "batch_id": batch_id, "case_id": case_id, "action": action},
    }


@app.get("/api/v1/aitest/cases")
def cases(
    batch_id: str = Query(...),
    x_aitest_read_token: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    _auth(x_aitest_read_token)
    data = yaml.safe_load(_SCENARIO.read_text(encoding="utf-8"))
    return {"batch_id": batch_id, "cases": data["cases"]}


@app.post("/api/v1/aitest/aitrace/status")
def receive_status(
    payload: dict[str, Any],
    x_aitest_read_token: Annotated[str | None, Header()] = None,
) -> dict[str, bool]:
    _auth(x_aitest_read_token)
    _callbacks.append(payload)
    return {"ok": True}


@app.get("/__mock/callbacks")
def callbacks(x_aitest_read_token: Annotated[str | None, Header()] = None) -> dict[str, Any]:
    _auth(x_aitest_read_token)
    return {"items": list(_callbacks)}


@app.post("/__mock/reset")
def reset(x_aitest_read_token: Annotated[str | None, Header()] = None) -> dict[str, bool]:
    _auth(x_aitest_read_token)
    _callbacks.clear()
    return {"ok": True}
```

- [ ] **Step 4: Run mock tests**

Run: `pytest tests/contract/test_aitest_mock.py -v`

Expected: 4 passed.

- [ ] **Step 5: Commit checkpoint**

```bash
git add mocks/aitest tests/contract/test_aitest_mock.py
git commit -m "feat: add isolated aitest mock process"
```

---

### Task 13: End-to-end smoke and architecture guard

**Files:**
- Create: `tests/e2e/test_delivery_to_queue.py`
- Test: `tests/unit/test_architecture.py`
- Modify: `mocks/aitest/README.md` (scenario switching notes)

**Interfaces:**
- Produces: an e2e test that starts the mock as a subprocess, delivers a failure package to the real app, and reads back `QUEUED/10`; an import-boundary test.

- [ ] **Step 1: Write the failing e2e and architecture tests**

```python
# tests/e2e/test_delivery_to_queue.py
import socket
import subprocess
import sys
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from aitrace.api.app import create_app
from aitrace.core.settings import Settings
from tests.integration.test_delivery_api import HEADERS, _payload

pytestmark = pytest.mark.e2e


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture(scope="module")
def mock_url() -> str:
    port = _free_port()
    process = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "aitest_mock.main:app", "--port", str(port), "--log-level", "warning"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    url = f"http://127.0.0.1:{port}"
    for _ in range(50):
        try:
            if httpx.get(f"{url}/api/v1/health", timeout=0.5).status_code == 200:
                break
        except httpx.HTTPError:
            time.sleep(0.1)
    else:
        process.terminate()
        raise RuntimeError("aitest mock did not start")
    yield url
    process.terminate()
    process.wait(timeout=10)


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
```

```python
# tests/unit/test_architecture.py
import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def test_mock_never_imports_aitrace() -> None:
    for path in (ROOT / "mocks/aitest/src").rglob("*.py"):
        names = _imports(path)
        assert not any(name == "aitrace" or name.startswith("aitrace.") for name in names), path


def test_persistence_does_not_import_api() -> None:
    for path in (ROOT / "src/aitrace/persistence").rglob("*.py"):
        names = _imports(path)
        assert not any(name == "aitrace.api" or name.startswith("aitrace.api.") for name in names), path
```

- [ ] **Step 2: Run and confirm failure**

Run: `pytest tests/e2e/test_delivery_to_queue.py tests/unit/test_architecture.py -v`

Expected: e2e PASS if Tasks 9–12 are complete; architecture PASS. If either fails, fix the boundary or the missing wiring before continuing.

- [ ] **Step 3: Add scenario switching notes**

Append to `mocks/aitest/README.md`:

```markdown
## 场景切换

- 请求头 `X-Mock-Scenario: token_not_found` 返回 404 `TOKEN_NOT_FOUND`
- 请求头 `X-Mock-Scenario: token_expired` 返回 410 `TOKEN_EXPIRED`
- `POST /__mock/reset` 清空已接收回调，供 e2e 用例隔离
- 所有 `/api/v1/aitest/*` 与 `/__mock/*` 端点要求 `X-Aitest-Read-Token`，默认 `mock-read-token`
```

- [ ] **Step 4: Run the full e2e chain**

Run: `make e2e`

Expected: e2e tests pass.

- [ ] **Step 5: Commit checkpoint**

```bash
git add tests/e2e tests/unit/test_architecture.py mocks/aitest/README.md
git commit -m "test: add delivery-to-queue e2e and import boundary guard"
```

---

### Task 14: Linux deployment skeleton

**Files:**
- Create: `deploy/systemd/aitrace.service`
- Create: `deploy/logrotate/aitrace`
- Create: `deploy/config/config.dev.yaml`
- Create: `deploy/config/config.prod.example.yaml`
- Create: `deploy/env/aitrace.env.example`
- Create: `deploy/scripts/install_release.sh`
- Create: `deploy/scripts/upgrade.sh`
- Create: `deploy/scripts/rollback.sh`
- Create: `deploy/scripts/migrate.sh`
- Create: `deploy/scripts/healthcheck.sh`
- Test: `tests/ops/test_deploy_assets.py`

**Interfaces:**
- Produces: systemd unit with tmpfs HOME and credentials; logrotate policy; config templates; release scripts that validate layout and never embed secrets.

- [ ] **Step 1: Write the failing deployment asset tests**

```python
# tests/ops/test_deploy_assets.py
import subprocess
from pathlib import Path

import pytest

from aitrace.core.settings import load_settings

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = sorted((ROOT / "deploy/scripts").glob("*.sh"))


@pytest.mark.parametrize(
    "relative",
    [
        "deploy/systemd/aitrace.service",
        "deploy/logrotate/aitrace",
        "deploy/config/config.dev.yaml",
        "deploy/config/config.prod.example.yaml",
        "deploy/env/aitrace.env.example",
        "deploy/scripts/install_release.sh",
        "deploy/scripts/upgrade.sh",
        "deploy/scripts/rollback.sh",
        "deploy/scripts/migrate.sh",
        "deploy/scripts/healthcheck.sh",
    ],
)
def test_required_asset_exists(relative: str) -> None:
    assert (ROOT / relative).is_file()


def test_shell_scripts_parse() -> None:
    assert SCRIPTS
    for script in SCRIPTS:
        subprocess.run(["bash", "-n", str(script)], check=True)


def test_unit_uses_release_entrypoint_and_tmpfs_home() -> None:
    text = (ROOT / "deploy/systemd/aitrace.service").read_text(encoding="utf-8")
    assert "ExecStart=/opt/aitrace/current/venv/bin/aitrace-server" in text
    assert "Environment=AITRACE_DATA_DIR=/var/lib/aitrace" in text
    assert "Environment=HOME=/run/aitrace/home" in text
    assert "RuntimeDirectory=aitrace" in text
    assert "TimeoutStopSec=120s" in text
    assert "User=aitrace" in text
    assert "User=root" not in text
    assert "AITRACE_SERVICE_TOKEN=" not in text


def test_prod_example_config_is_valid(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    token_file = tmp_path / "service-token"
    token_file.write_text("example-token\n", encoding="utf-8")
    monkeypatch.delenv("AITRACE_SERVICE_TOKEN", raising=False)
    monkeypatch.setenv("AITRACE_SERVICE_TOKEN_FILE", str(token_file))
    settings = load_settings(ROOT / "deploy/config/config.prod.example.yaml")
    assert settings.env == "prod"
    assert settings.service_token is not None
    assert settings.service_token.get_secret_value() == "example-token"
    assert settings.storage.profile_domains_dir == Path(
        "/opt/aitrace/current/deploy/config/domains"
    )
```

- [ ] **Step 2: Run and confirm failure**

Run: `pytest tests/ops/test_deploy_assets.py -v`

Expected: FAIL because deployment assets do not exist.

- [ ] **Step 3: Add systemd, logrotate, and config templates**

```ini
# deploy/systemd/aitrace.service
[Unit]
Description=AITrace failure analysis service
After=network-online.target postgresql.service
Wants=network-online.target

[Service]
Type=simple
User=aitrace
Group=aitrace
WorkingDirectory=/opt/aitrace/current
Environment=AITRACE_DATA_DIR=/var/lib/aitrace
Environment=HOME=/run/aitrace/home
Environment=PATH=/opt/aitrace/current/bin:/usr/local/bin:/usr/bin:/bin
Environment=AITRACE_SERVICE_TOKEN_FILE=%d/service-token
Environment=AITRACE_DATABASE__DSN=postgresql+psycopg://aitrace@127.0.0.1:5432/aitrace
Environment=AITRACE_STORAGE__PROFILE_DOMAINS_DIR=/opt/aitrace/current/deploy/config/domains
RuntimeDirectory=aitrace
RuntimeDirectoryMode=0700
LoadCredential=service-token:/etc/aitrace/credentials/service-token
ExecStart=/opt/aitrace/current/venv/bin/aitrace-server
Restart=on-failure
RestartSec=5s
TimeoutStopSec=120s
LimitNOFILE=65536
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
```

```ini
# deploy/logrotate/aitrace
/var/lib/aitrace/logs/*.log {
    daily
    rotate 30
    compress
    delaycompress
    missingok
    notifempty
    copytruncate
    su aitrace aitrace
}
```

```yaml
# deploy/config/config.dev.yaml
env: dev
server:
  host: 127.0.0.1
  port: 8080
  log_level: DEBUG
database:
  dsn: postgresql+psycopg://aitrace:aitrace@127.0.0.1:5432/aitrace
  pool_size: 5
queue:
  cleaning_concurrency: 2
  ai_worker_concurrency: 2
storage:
  data_dir: data
  profile_domains_dir: deploy/config/domains
```

```yaml
# deploy/config/config.prod.example.yaml
env: prod
server:
  host: 127.0.0.1
  port: 8080
  log_level: INFO
database:
  dsn: postgresql+psycopg://aitrace:CHANGE_ME@127.0.0.1:5432/aitrace
  pool_size: 20
queue:
  cleaning_concurrency: 4
  ai_worker_concurrency: 4
  lease_seconds: 600
  ai_hard_timeout_seconds: 1800
storage:
  data_dir: /var/lib/aitrace
  max_attachment_bytes: 52428800
  profile_domains_dir: /opt/aitrace/current/deploy/config/domains
```

```bash
# deploy/env/aitrace.env.example
# 仅放非敏感运行参数。凭证走 LoadCredential / KMS，禁止写入本文件。
AITRACE_SERVER__LOG_LEVEL=INFO
```

- [ ] **Step 4: Add release and operations scripts**

```bash
#!/usr/bin/env bash
# deploy/scripts/install_release.sh
set -euo pipefail
src="${1:?usage: install_release.sh <release-dir>}"
revision="$(cat "${src}/REVISION")"
target="/opt/aitrace/releases/${revision}"
[[ -f "${src}/DB_COMPAT" ]] || { echo "missing DB_COMPAT" >&2; exit 1; }
install -d -m 0755 "${target}"
cp -a "${src}/." "${target}/"
python3.13 -m venv "${target}/venv"
"${target}/venv/bin/pip" install --upgrade pip
"${target}/venv/bin/pip" install --no-index --find-links "${target}/wheels" "aitrace==${revision}"
ln -sfn "${target}" /opt/aitrace/current
systemctl daemon-reload
systemctl enable aitrace.service
```

```bash
#!/usr/bin/env bash
# deploy/scripts/upgrade.sh
set -euo pipefail
release="${1:?usage: upgrade.sh <release-dir>}"
previous="$(readlink -f /opt/aitrace/current)"
deploy/scripts/install_release.sh "${release}"
deploy/scripts/migrate.sh "/opt/aitrace/current"
systemctl restart aitrace.service
if ! deploy/scripts/healthcheck.sh; then
  echo "health check failed, rolling back" >&2
  deploy/scripts/rollback.sh "${previous}"
  exit 1
fi
```

```bash
#!/usr/bin/env bash
# deploy/scripts/rollback.sh
set -euo pipefail
target="${1:?usage: rollback.sh <release-dir>}"
[[ -d "${target}" ]] || { echo "release not found: ${target}" >&2; exit 1; }
ln -sfn "${target}" /opt/aitrace/current
systemctl restart aitrace.service
```

```bash
#!/usr/bin/env bash
# deploy/scripts/migrate.sh
set -euo pipefail
release="${1:?usage: migrate.sh <release-dir>}"
[[ -f "${release}/alembic.ini" ]] || { echo "missing alembic.ini" >&2; exit 1; }
"${release}/venv/bin/alembic" -c "${release}/alembic.ini" upgrade head
```

```bash
#!/usr/bin/env bash
# deploy/scripts/healthcheck.sh
set -euo pipefail
curl --fail --silent --show-error --max-time 5 http://127.0.0.1:8080/api/v1/health >/dev/null
```

- [ ] **Step 5: Run deployment asset tests**

Run: `pytest tests/ops/test_deploy_assets.py -v`

Expected: 13 passed.

- [ ] **Step 6: Commit checkpoint**

```bash
git add deploy tests/ops/test_deploy_assets.py
git commit -m "feat: add Linux deployment skeleton"
```

---

### Task 15: M0 verification and review

**Files:**
- Modify: `README.md` (add plan links and M0 status)
- Test: full suite

**Interfaces:**
- Produces: a passing M0 gate and review evidence. No new behavior.

- [ ] **Step 1: Run the full verification**

```bash
make lint
make test
make contract
make e2e
```

Expected: lint clean; all tests pass; coverage ≥ 85%; contract check exits 0; e2e passes.

- [ ] **Step 2: Verify the secret boundary manually**

```bash
git grep -n -E "qoder_access_token|access_token" -- ':!tests/integration/test_delivery_api.py' ':!docs/' ':!README.md'
```

Expected: no matches outside the rejection test and documentation.

```bash
git grep -n "AITRACE_SERVICE_TOKEN=" deploy/
```

Expected: no match; only `AITRACE_SERVICE_TOKEN_FILE` appears.

- [ ] **Step 3: Update README with plan links and M0 status**

Add to the document table:

```markdown
| [docs/superpowers/plans/2026-11-25-aitrace-v1-program.md](docs/superpowers/plans/2026-11-25-aitrace-v1-program.md) | V1 总体实施计划（M0–M5 边界、接口、验证矩阵） |
| [docs/superpowers/plans/2026-11-25-aitrace-m0-foundation.md](docs/superpowers/plans/2026-11-25-aitrace-m0-foundation.md) | M0 契约冻结与骨架详细 TDD 计划 |
```

- [ ] **Step 4: Request review**

Dispatch a `general-purpose` reviewer with:

- Description: "M0 foundation: settings, profiles, schema, audit chain, queue/lease/slot gate, delivery ingestion, session query, mock, deploy skeleton"
- Requirements: this plan plus spec §3.4, §4.5, §5.1, §6.4–§6.5.5, §8.2, §8.4, §10.3
- Base/head SHAs after the phase commit

Fix Critical and Important findings before declaring M0 complete.

- [ ] **Step 5: Commit checkpoint**

```bash
git add README.md
git commit -m "docs: link V1 program and M0 implementation plans"
```

---

## M0 Acceptance Checklist

- [ ] `POST /api/v1/aitrace/tasks` accepts the spec payload, returns 202/200 correctly, and rejects unknown fields including any token.
- [ ] Duplicate delivery returns the existing session without creating a second round or cleaning task.
- [ ] Every accepted case creates exactly one `round` and one `PENDING` `case_clean` task, and exactly one active `user_queue` row.
- [ ] `GET /api/v1/aitrace/tasks/{session_id}` returns `QUEUED/10` with a valid `waiting_reason`.
- [ ] Cleaning claims use `FOR UPDATE SKIP LOCKED`, increment `lease_generation`, and reject stale writers.
- [ ] AI claims increment `user_queue.in_flight` and set `AI_RUNNING/JIT_FETCH` in one transaction; finish/reclaim releases exactly once.
- [ ] `PAUSED_AUTH` and future `paused_until` block the slot.
- [ ] `audit_log` rejects UPDATE/DELETE and `verify_chain` detects forged rows.
- [ ] Unknown config fields, out-of-range values, and missing service tokens stop startup with Chinese field-level messages.
- [ ] Unknown products fall back to the default profile; profile versions are deterministic.
- [ ] `mocks/aitest` has no import of `aitrace`.
- [ ] Deployment assets parse, the unit is non-root with tmpfs HOME and LoadCredential, and no secret value is committed.
- [ ] `make lint`, `make test`, `make contract`, `make e2e` all pass.

## Handoff to M1

M1 consumes `QueueRepository.claim_clean`/`finish`, writes `CleaningArtifact` to L1, and moves tasks to `READY` or `FAILED`. It must not change the delivery contract, the task state names, or the slot-gate semantics established here.

---

## 执行记录（2026-11-26，M0 实施完成）

实施采用 SDD 子代理逐任务执行 + 控制器内联评审（用户裁定：不派评审子代理、不自动提交）。全部 15 个任务完成；四道门禁全绿（见程序计划执行状态表）。

**对计划文本的裁定与修正**（实施期发现，未改接口语义）：

1. Task 2：令牌文件读取失败与节段级类型错误补齐中文字段诊断（原计划仅顶层）；令牌不落日志有回归测试锁定。
2. Task 4：任务自相矛盾（测试插入两行相同 T1/T2 违反 `uq_task_case`）——schema 保持计划原文，测试改为 T2 用 round_no=2、T3 复现 T1 以断言唯一性。
3. Task 5/6：JSONB 归一化（`1.0→1`）会破坏审计哈希链——`AuditService.append` 拒绝浮点数（中文报错），spec 附录 B 已回填。
4. Task 8：`reclaim_confirmed` 需 `populate_existing=True`（ORM 身份映射陈旧性，CAS 正确性修复）；`rowcount` 需 `cast(CursorResult[Any], ...)` 过 mypy。
5. Task 9：投递契约按计划原文（`extra=forbid`、`hardware_type Literal["SSD","CXL"]`、IPv4 校验、设备绑定校验、幂等 200/新建 202）；令牌比较用普通 `!=`（计划原文，`compare_digest` 加固推迟）。
6. Task 12（用户指令增补）：aitest mock 增加极简 HTML 前端 + 投递转发 / 状态代理端点；httpx `trust_env=False`（宿主机畸形 proxy 环境变量导致 0.28 构造崩溃）。
7. Task 13（用户指令增补）：e2e 增加真实 HTTP 全链路用例——AITrace uvicorn（`create_app --factory`）+ mock uvicorn 双子进程，mock /deliver → 202 → 状态代理 → QUEUED/10；httpx `InvalidURL` 不继承 `HTTPError`，fixture 用 `BaseException` 兜底回收子进程。
8. 令牌门禁：实施期提供的 CN 令牌实测 401 `TOKEN_EXPIRE`（openapi.qoder.com.cn，2026-11-26）——真实 AI e2e 挂起，待有效令牌；令牌异常分类用真实拒绝 + Fake-Qoder 覆盖。
9. 本地环境：PostgreSQL 16.16（Homebrew）、双库、无 Docker；`git grep` 门禁在首次提交前须改用工作树 grep（实现树尚未跟踪）。
10. 提交纪律：全程未做任何 git 提交（用户约束：不自动提交、分阶段验收）。

**M1 移交**：接口与语义均按 Handoff 节冻结；M1 计划在开工时按本文件既有结构新写（程序计划约定逐里程碑成稿）。
