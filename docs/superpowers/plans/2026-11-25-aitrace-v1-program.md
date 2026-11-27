# AITrace V1 Implementation Program Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver the AITrace V1.2 failure-analysis sidecar end to end: aitest delivery → deterministic cleaning → per-token AI analysis → report/notification → feedback evolution → dashboards, with every feature covered by unit tests and a runnable verification gate.

**Architecture:** One Python process (`aitrace-server`) hosts the FastAPI inbound API, a bounded cleaning pool, a per-token AI queue with a DB slot gate, a shared AI worker pool, JIT token dispatch, and callback/notification workers. PostgreSQL is the only queue/broker; Qoder CLI is launched per task; evidence and raw logs stay on local disk. The code is a modular monolith with one-way imports and test doubles at every external boundary.

**Tech Stack:** Python 3.13, FastAPI, Pydantic v2, SQLAlchemy 2.0 (sync) + psycopg 3, PostgreSQL 16, Alembic, PyYAML, Qoder Python SDK + local CLI, pytest + pytest-cov, Ruff, mypy, GitHub Actions.

**Spec:** [docs/superpowers/specs/2026-11-25-aitrace-design-v1-final.md](../specs/2026-11-25-aitrace-design-v1-final.md)

## Global Constraints

- `qoder_access_token` is fetched JIT over HTTPS, held only in process memory during one task, and destroyed at task end. It must never appear in PostgreSQL, the task queue, Redis, caches, audit rows, config files, env files, systemd credentials, task payloads, or application logs.
- There is no platform fallback token. Missing or unauthorized tokens fail only the current task with `TOKEN_REQUIRED`; recoverable token failures pause only that user's queue with `PAUSED_AUTH`.
- The Qoder SDK is called with an in-memory `accessToken(token)` only. `accessTokenFromEnv()`, `qodercliAuth`, and persistent CLI login state are forbidden. Qoder task HOME is tmpfs and wiped.
- SSH probes are read-only and constrained to the intersection of the global command whitelist and the profile whitelist. No write, format, reset, firmware flash, or controller mutation command.
- V1 is one Linux machine, one `aitrace.service`, no container runtime, no `uvicorn --workers`, local PostgreSQL, and local Qoder CLI runtime. C/W are process-level bounded pools.
- Only the API `/api/v1` is public. Additive fields are allowed; removals require the two-release deprecation window in §6.2.
- All timestamps are UTC ISO-8601 in APIs and timezone-aware `TIMESTAMPTZ` in PostgreSQL.
- Every feature task follows red → green → refactor and adds unit tests. Integration, contract, e2e, security, perf, and ops gates are added at the milestone where the feature first becomes runnable.
- Local commits are made only at an approved phase boundary. Never push without an explicit request.
- The spec is the source of truth. If implementation needs a semantic change, update the spec and this plan in the same change.

## Scope Decision

The V1 spec contains five independently testable subsystems. A single monolithic plan would hide interface drift and make review impossible, so implementation is split into one program plan plus one detailed TDD plan per milestone. Each milestone plan produces working, testable software on its own.

| Milestone | Detailed plan | Working software produced | Acceptance gate |
|---|---|---|---|
| M0 Foundation | [2026-11-25-aitrace-m0-foundation.md](2026-11-25-aitrace-m0-foundation.md) | Service boots; contracts, config, profiles, DB schema, queue/slot gate, ingestion/query, mock and deploy skeleton work | `make test`, `make contract`, `make e2e` pass; M0 smoke: POST tasks → PENDING → query QUEUED |
| M1 Cleaning | Written at M1 kickoff | Ingest → deterministic cleaning → rule report; batch aggregation and knowledge tables | Stage-1 golden logs and first-error/slice/fingerprint tests pass |
| M2 AI | [2026-11-26-aitrace-m2-ai.md](2026-11-26-aitrace-m2-ai.md) | Clean → per-token queue → JIT token → Qoder main/sub-agent → confidence → result/callback | Fake-Qoder e2e covers success, token missing/expired, pause/recover, fencing |
| M3 Feedback | [2026-11-27-aitrace-m3-feedback.md](2026-11-27-aitrace-m3-feedback.md) | Notifications, two-state feedback, contribution ledger, hot memory, citation cascade, dashboards | Feedback→knowledge→dashboard e2e; citation cascade tests |
| M4 SLO/Ops | [2026-11-27-aitrace-m4-slo-ops.md](2026-11-27-aitrace-m4-slo-ops.md) | Metrics/alerts, watermarks, backup/restore, SLO calibration harness, full release scripts | RPO/RTO drill, rollback drill, calibration report |
| M5 Hardening | Written at M5 kickoff | Security/perf regression, gray release, runbooks | Red-team, perf, contract regression, compliance gate |

This is intentional: detailed plan files are created immediately before their milestone so interfaces proven in the previous milestone are reflected, instead of freezing unvalidated code for eight weeks.

## Execution Status

| Milestone | Status | Evidence |
|---|---|---|
| M0 Foundation | **DONE 2026-11-26** | `make lint` clean · `make test` 99 passed（cov 96.45%）· `make contract` 14 passed · `make e2e` 2 passed（真实 HTTP 全链路 mock→AITrace：deliver 202 → 状态查询 QUEUED/10）。执行裁定与实测回填见 spec 附录 B「V1.2 实施修订（2026-11-26）」与 M0 计划尾部执行记录。 |
| M1 Cleaning | **DONE 2026-11-26** | `make lint` clean · `make test` 154 passed（cov 95.93%）· `make contract` 14 passed · `make e2e` 2 passed；迁移 0002（batch.gate）升降级实测通过。执行裁定见 [2026-11-26-aitrace-m1-cleaning.md](2026-11-26-aitrace-m1-cleaning.md) 尾部执行记录。 |
| M2 AI | **DONE 2026-11-26** | `make verify` 全绿（`make lint` 141 文件 / mypy 75 源文件 · `make test` 238 passed（cov 95.28%）· `make contract` 17 passed · `make e2e` 2+2 passed）；迁移 0002↔0005 全链双向升降级实测通过；Fake-Qoder 全链（deliver→clean→route→AI→release→callback→batch_close）与令牌缺失/过期暂停恢复链 e2e 归绿；令牌红线审计通过。端到端系统级验证（真实 HTTP）：Fake 版正向全链批次 COMPLETED + RESULT 回调 + 状态 API 完整报告；真实 SDK（cn + GLM-5.3-Flash）负向全链批次 FAILED、状态 API stage=FAILED；修复轮 R2 收敛 D1/D2/D3 状态语义缺陷（详见 M2 计划文末「端到端验证记录」）。执行裁定见 [2026-11-26-aitrace-m2-ai.md](2026-11-26-aitrace-m2-ai.md) 文末执行记录。真实模型（GLM-5.3-Flash）验证需有效 CN 令牌：实施期提供的令牌 2026-11-26 三次独立实测均 401 TOKEN_INVALID（invalid pt-token）。 |
| M3 Feedback | **DONE 2026-11-27** | `make verify` 全绿（`make lint` 清洁（mypy 90 源文件）· `make test` 341 passed（cov 95.05%）· `make contract` 26 passed · `make e2e` 4 passed）；M3 无新增 schema 迁移（沿用 0001–0005）；系统级 fake 链真实 HTTP 走查「投递 → COMPLETED(FW_DEFECT, 0.78) → 反馈 VALID → 案例库/热记忆物化 → 看板 case_count 0→1」，NOTIFY 三卡（诊断/高危 AER/终态 COMPLETED）经 mock `/__mock/cards` 捕获，浏览器实测控制台与看板新数据（Playwright 截图/快照取证）；SSH 只读探针 31 例安全测试先行全绿（白名单交集/拒绝审计/哈希链 verify）。执行裁定见 [2026-11-27-aitrace-m3-feedback.md](2026-11-27-aitrace-m3-feedback.md) Task 7/8 实施裁决及文末执行记录，spec 附录 B「V1.2 实施修订（2026-11-27）」同步回填。 |
| M4 SLO/Ops | **DONE 2026-11-27** | `make verify` 全绿（`make lint` 清洁（mypy 102 源文件）· `make test` 445 passed / 2 skipped（cov 94.43%）· `make contract` 26 passed · `make e2e` 4 passed）；`make test-security` 41 passed / 44 skipped（203 deselected）；`make ops` 20 passed（含真实 pg_dump/pg_restore 备份演练，`AITRACE_OPS_DRILL=1` 门禁）；`make perf` 快配复跑全绿（2026-11-27T09:43Z：burndown 8/8、fairness 11/11、backpressure 投递 20/受理 10/429×10）。T1–T7 落地观测底座/水位背压/监控看板/告警触达/readyz+自检/备份 CLI+timer/六步发布；T8 perf harness 实跑逼出三门产品缺陷并 TDD 修复。SLO 基线回填 spec §7.1「实测（P95）」列与 §8.2 C/W/D 收敛值（C=4、W=2、D=360s），附录 B 追加 M4 实施修订。执行记录与裁决见 [2026-11-27-aitrace-m4-slo-ops.md](2026-11-27-aitrace-m4-slo-ops.md) 文末。 |
| M5 Hardening | **DONE 2026-11-27** | `make verify` 全绿（`make lint` 清洁（mypy 103 源文件）· `make test` 486 passed / 2 skipped（cov 94.40%）· `make contract` 26 passed · `make e2e` 4 passed）；`make test-security`（带 TEST_DATABASE_URL）97 passed / 0 skipped（203 deselected）；`make ops` 20 passed（含 2 项真实 pg_dump/pg_restore 演练）；`make perf --out` 三 profile 全 PASS（backpressure 投递 20 / 受理 10 / 429 × 10，retry_after P95 60s）。T1–T4 落地四道安全闸（主机资产范围/灰度投递/附件三道门/JIT 拉取入审计）；T5 红队用例四套（注入/探针/附件/令牌）入 `make verify`；T6 运维手册五件套（deploy/runbooks/）+ R1 合规评估报告（条件放行 gate）；T7 全量回归与 spec 附录 B「V1.3 实施修订」回填。执行记录与裁决见 [2026-11-27-aitrace-m5-hardening.md](2026-11-27-aitrace-m5-hardening.md) 文末。 |

## Cross-Milestone Interfaces

The following names and signatures are fixed across plans. A milestone may add fields, but may not rename these without updating every plan and the spec.

### Domain Types

```python
# src/aitrace/core/enums.py
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
```

### Ports and Services

```python
class Clock(Protocol):
    def now(self) -> datetime: ...

class IdGenerator(Protocol):
    def new(self, prefix: str) -> str: ...

class ProfileResolver(Protocol):
    def resolve(self, product_id: str) -> ResolvedProfile: ...

class AitestClient(Protocol):
    def fetch_token(self, *, product_id: str, batch_id: str, case_id: str,
                    trigger_user_id: str, action: str) -> TokenLease: ...
    def download_log(self, *, path: str, failed_at_utc: int,
                     byte_range: tuple[int, int] | None = None) -> LogChunk: ...
    def list_logs(self, *, batch_id: str, case_id: str) -> list[LogDescriptor]: ...
    def list_cases(self, *, batch_id: str) -> list[CaseDescriptor]: ...
    def get_batch_env(self, *, batch_id: str) -> BatchEnvironment: ...
    def get_notify_config(self, *, product_id: str) -> NotifyConfig: ...
    def push_status(self, *, payload: CallbackPayload) -> None: ...

class QoderRuntime(Protocol):
    def analyze(self, request: AnalysisRequest, token: SecretStr) -> ReportDraft: ...

class ProbeClient(Protocol):
    def run(self, *, host: str, command: str) -> ProbeResult: ...

class QueueRepository(Protocol):
    def enqueue_clean(self, task: NewTask) -> TaskQueueRow: ...
    def claim_clean(self, *, owner: str) -> TaskQueueRow | None: ...
    def claim_ai(self, *, owner: str) -> TaskQueueRow | None: ...
    def finish(self, *, task_id: str, owner: str, generation: int,
               state: QueueState, error: dict | None = None) -> bool: ...

class AuditService(Protocol):
    def append(self, *, actor: str, action: str, target: str,
               detail: dict) -> None: ...
    def verify_chain(self) -> bool: ...
```

### Milestone Seams

- **M0 → M1:** M0 writes `case_clean` tasks in `PENDING` and exposes `QueueRepository.claim_clean`/`finish`. M1 only adds the cleaner plugin registry and calls `finish(..., READY | FAILED)`.
- **M1 → M2:** M1 writes a `CleaningArtifact` to L1 and stores its path/hash in the task payload; M2 does not re-read raw logs for analysis.
- **M2 → M3:** M2 writes the final report transactionally and emits callback tasks; M3 consumes the same `Report` contract for notifications and feedback.
- **M3 → M4:** M3 exposes count/ratio queries used by dashboards and monitoring; M4 does not add new business tables.
- **All → M5:** M5 may add indexes and caches but must not change API or queue semantics without a spec revision.

## File Ownership

The directory boundaries from spec §10.3 are binding:

| Root | Owns | Must not contain |
|---|---|---|
| `src/aitrace/` | production code, migrations, prompts, profile runtime | tests, mocks, sample logs |
| `contracts/` | OpenAPI and shared JSON Schema | implementation, environment values |
| `mocks/aitest/` | aitest fake service, scenarios, fake tokens | imports from `src/aitrace` |
| `deploy/` | config templates, domains, skills, systemd, scripts | real secrets, runtime data |
| `data/` | local L1/L2/runtime/logs/backups | committed files except README |
| `tests/` | unit/integration/contract/e2e/security/perf/ops | production imports from tests |

## Verification Matrix

| Gate | Command | Blocks |
|---|---|---|
| Format/lint/types | `make lint` (`ruff check`, `ruff format --check`, `mypy src`) | every merge |
| Unit + coverage | `make test` (full suite with `--cov-fail-under=85`); `make test-unit` for the fast loop | every merge |
| Integration | `make test-integration` with `TEST_DATABASE_URL` | every merge |
| Contract | `make contract` (OpenAPI/JSON Schema drift, no token fields) | J0 onwards, every merge |
| E2E | `make e2e` (local PG + mock subprocess + ASGI app) | nightly and release |
| Security | `make test-security` (token scan, auth, probe whitelist, injection) | M2 onwards, release |
| Perf | `make perf` (M4/J2 scenarios) | M4 and before gray |
| Ops | `make ops` (backup/restore, release/rollback, startup checks) | M4 onwards |

Coverage is not a substitute for behavior: every functional requirement must have at least one named test that fails if the behavior is removed. `make test-unit` is the default developer loop; the full gate is `make verify`.

## Execution Rules

1. Write the failing test first. Run it and record the expected failure.
2. Implement the minimum code to pass. Do not add speculative abstractions.
3. Run the focused test, then the milestone suite.
4. Run `make lint` and `make verify` before a phase review.
5. At a phase boundary, present the diff and test evidence; commit locally only after explicit approval; never push.
6. If a spec assumption fails, stop and update the spec/plan before changing code.

## Definition of Done (V1)

- The M0–M5 acceptance gates all pass on a clean Linux host with local PostgreSQL.
- The end-to-end path works with the aitest mock: delivery → cleaning → per-token AI → report → callback → feedback → knowledge update → dashboard.
- Unit tests cover every functional requirement and every error branch named in §6.5.5.
- No test or runtime path can persist a Qoder token; the security scan is part of CI.
- The deployment can install, start, stop, upgrade, roll back, back up, and restore using `deploy/scripts/` and the systemd units.
- The final spec and all milestone plans are consistent; no undocumented behavior exists.
