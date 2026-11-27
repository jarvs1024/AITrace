# AITrace M4 SLO/Ops Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver the M4 SLO/Ops milestone: structured-triple-ledger logging with stage pipeline events, in-process metrics, watermarks with three-level L2 disk management and QUEUE_FULL/DISK_FULL backpressure, monitoring snapshot + dashboard view ⑥, threshold alerting loop, `/readyz` + fail-fast startup checks + graceful drain, backup/restore CLI with systemd timer, complete upgrade/rollback scripts, `make perf` calibration harness, `make ops`/`make test-security` gates, and SLO baseline write-back.

**Architecture:** One process (`aitrace-server`) hosts the API, dispatcher, and a new monitor loop. Observability is split into two layers: an in-process `MetricsRegistry` (counters/histograms/gauges consumed by the real-time monitoring console and the alert loop) plus structured JSON logs (the SLS aggregation source). Watermark checks run at ingest acceptance and in a cleaner that evicts L2/L1 files in priority order. `aitrace-backup` is a Python CLI (testable, Chinese self-explaining errors) driven by `aitrace-backup.timer`; release scripts are rewritten to the §8.5 six-step protocol with `/readyz` polling and 3-release retention. No Prometheus; no new business tables; no new migrations (per program-plan seam M3→M4).

**Tech Stack:** Python 3.13, FastAPI, Pydantic v2, SQLAlchemy 2.0, PostgreSQL 16, pg_dump/pg_restore via subprocess, systemd timer, httpx (alert webhook), pytest + pytest-cov, Ruff, mypy, bash for deploy scripts.

**Spec:** [docs/superpowers/specs/2026-11-25-aitrace-design-v1-final.md](../specs/2026-11-25-aitrace-design-v1-final.md) — §5.3 存储分层与三级水位, §7.1 两阶段标定, §7.3 监控与告警, §7.4 日志设计, §8.2 配置管理, §8.4 进程模型/启动自检, §8.5 安装发布回滚, §8.6 备份策略, §9 R11–R18。

## Global Constraints

- All timestamps are UTC ISO-8601 in APIs and timezone-aware `TIMESTAMPTZ` in PostgreSQL.
- Token red line: no `qoder_access_token` (or any secret) may enter logs, DB, queue, or files; structured logs MUST mask token/secret/password-like fields and values; error messages never echo secret file contents.
- Error messages are self-explaining (Chinese field names, current value, allowed values), per the `{code, message, retryable, retry_after?}` unified body of §6.5.5: `QUEUE_FULL` HTTP 429, `DISK_FULL` HTTP 507, both `retryable=true` with `retry_after`.
- V1 is one Linux machine, one `aitrace.service`, no container runtime, no `uvicorn --workers`; C/W are process-level bounded pools. No Prometheus/Grafana: structured logs + aggregation + dashboard ⑥.
- M4 does not add new business tables or migrations: stage timings come from task-flow log events + in-process histograms; current waits from existing rows.
- Only the API `/api/v1` is public; additive fields allowed; contract export must keep passing (no token fields).
- Every task follows red → green → refactor and adds named unit tests. Gate before phase review: `make lint`, `make verify`; M4 additionally `make test-security`, `make ops`, `make perf`.
- Local commits only at task boundaries with green evidence; push only at approved phase boundaries (this milestone: user authorized stage pushes).
- The spec is the source of truth. If implementation needs a semantic change, update the spec (§8.2 SLO params, §7.1 calibration status) and this plan in the same change.

## 现状盘点（M0–M3 已交付 / M4 缺口）

| 已交付 | 位置 |
|---|---|
| 配置加载与启动强校验（Schema 级） | `src/aitrace/core/settings.py` |
| 审计哈希链追加 + `verify_chain` | `src/aitrace/core/audit.py` |
| 看板聚合（overview/by_product/root_cause/knowledge/runtime/置信度直方图） | `src/aitrace/api/routes/dashboard.py` |
| `/api/v1/health` | `src/aitrace/api/routes/health.py` |
| 统一错误体 `{code,message,retryable,retry_after}` | `src/aitrace/api/errors.py` |
| 队列状态迁移单一咽喉（`QueueRepository` 全部 write 更新 `updated_at`） | `src/aitrace/persistence/repositories/queue.py` |
| deploy 骨架：healthcheck/install_release/migrate/rollback/upgrade、aitrace.service、logrotate、env 示例 | `deploy/` |
| ops 资产烟测（存在性/语法/可执行/单元禁越权） | `tests/ops/test_deploy_assets.py` |
| e2e 双链（fake 正向 / 令牌异常）、契约 26、单元 341、cov 95.05% | `tests/` |

| M4 缺口 | 依据 |
|---|---|
| 无结构化 JSON 日志/三本账/trace 上下文/任务流水阶段事件；无掩码装置 | spec §7.4 |
| 无进程内指标采集（18 类 §7.3 指标无载体） | spec §7.3 |
| `QUEUE_FULL`/`DISK_FULL` 背压全库未实现（grep 零命中）；无磁盘水位检查与 L2 清理器 | spec §4.5.2/§5.3/§6.5.5 |
| 无 `/readyz`、无启动自检五查、无优雅排水的 ready 语义 | spec §8.4 |
| 无备份脚本/CLI、无 `aitrace-backup.timer`、无恢复演练 | spec §8.6 |
| `upgrade.sh` 缺预检/优雅停服/readyz 轮询/release 保留；`install_release.sh` 提前动 `current` 链接 | spec §8.5 |
| `make perf` / `make ops` / `make test-security` Makefile 目标缺失 | 程序计划 Verification Matrix |
| 告警无触达（运维告警群 webhook 未实现） | spec §7.3/§8.2 |

## Cross-Task Fixed Interfaces

The following names/signatures are fixed across tasks. A task may add fields, not rename.

```python
# src/aitrace/observability/metrics.py
class Counter:
    def inc(self, n: int = 1) -> None: ...
class Histogram:
    def observe(self, value: float) -> None: ...
    def snapshot(self) -> HistogramSnapshot: ...   # count/sum/p50/p95/p99
class Gauge:
    def set(self, value: float) -> None: ...
    def add(self, delta: float) -> None: ...
class MetricsRegistry:
    def counter(self, name: str, *, unit: str = "") -> Counter
    def histogram(self, name: str, *, buckets_seconds: tuple[float, ...] = _DEFAULT_BUCKETS) -> Histogram
    def gauge(self, name: str, *, unit: str = "") -> Gauge
    def window_rate(self, name: str, window_seconds: float) -> float  # 滑动速率（内置 60s 桶）
    def snapshot(self) -> dict[str, Any]

# src/aitrace/observability/logging.py
def configure_logging(level: str) -> None                    # 全局 JSON 日志 + 掩码
def log_event(component: str, event: str, *, level: str, **fields: Any) -> None

# src/aitrace/observability/pipeline.py
def record_stage_event(registry: MetricsRegistry, *, task_type: str, queue_key: str,
                       from_state: str, to_state: str, duration_seconds: float) -> None
# 指标名：task_stage_duration_seconds{task_type,queue_key,from_state,to_state}；
# 同时 log_event(component="pipeline", event="task_stage", ...)

# src/aitrace/observability/monitor.py
def build_monitor_snapshot(session: Session, registry: MetricsRegistry,
                           settings: Settings, now: datetime) -> dict[str, Any]
# DB 侧含：global/per-token 队列深度、READY 任务等待时长（now - updated_at）按 queue_key、
# PAUSED_AUTH 计数与持续时长、callback 积压/失败、fencing 拒绝（registry）、
# 水位状态（watermark 模块）、Worker 槽位占用、审计链校验结果。

# src/aitrace/observability/alerts.py
class Alert(BaseModel):
    name: str; severity: Literal["INFO", "WARN", "CRITICAL"]
    threshold: str; current: str; message: str
def evaluate_alerts(snapshot: dict[str, Any], settings: Settings, now: datetime) -> list[Alert]
def send_alerts(alerts: list[Alert], *, ops_webhook: str | None,
                poster: Callable[[str, str], None] | None = None) -> None

# src/aitrace/storage/watermark.py
class WatermarkLevel(StrEnum): OK = "OK"; WARN = "WARN"; FORCE = "FORCE"
def disk_fraction(data_dir: Path, l2_root: Path, quota_bytes: int | None) -> float
def level_for(fraction: float, warn: float, force: float) -> WatermarkLevel
def evict(session: Session, settings: Settings, *, force: bool,
          clock: Clock, now: datetime) -> EvictionReport
@dataclass(frozen=True) class EvictionReport: freed_bytes: int; removed_dirs: int; by_reason: dict[str, int]

# src/aitrace/ops/startup.py
def run_startup_checks(settings: Settings, session_factory: sessionmaker[Session]) -> None
# 五查，任一失败抛 StartupError（中文自解释 + 修复提示）；含凭据文件扫描

# src/aitrace/ops/backup.py
def run_backup(settings: Settings, *, staging: Path, keep: int,
               clock: Clock) -> BackupReport   # 失败抛 BackupError（中文）；空间不足 SpaceShortageError
def run_restore(settings: Settings, dump_path: Path) -> None
```

Settings additions (all with Chinese `_FIELD_LABELS` entries, `extra="forbid"`):

```python
class SloSettings(BaseModel):
    ai_p95_baseline_seconds: int = Field(default=360, ge=60)          # D，初始 6min
    queue_to_clean_p95_seconds: int = Field(default=120, ge=10)
    e2e_first_case_p95_seconds: int = Field(default=900, ge=60)
    e2e_queued_case_p95_seconds: int = Field(default=2700, ge=60)
    e2e_batch_p95_seconds: int = Field(default=3600, ge=60)
    fairness_max_gap_seconds: int = Field(default=720, ge=60)         # 2×D
    probe_max_age_minutes: int = Field(default=30, ge=1)

class StorageSettings:  # 扩展字段（M4 在其上追加）
    l2_quota_bytes: int | None = Field(default=None, ge=0)            # None=按整盘比例
    l1_ttl_days: int = Field(default=30, ge=1)
    l2_ttl_days: int = Field(default=30, ge=1)
    watermark_warn_fraction: float = Field(default=0.70, gt=0, lt=1)
    watermark_force_fraction: float = Field(default=0.85, gt=0, lt=1)

class MonitorSettings(BaseModel):
    enabled: bool = True
    check_interval_seconds: float = Field(default=60.0, ge=5.0, le=3600)
    global_depth_warn_fraction: float = Field(default=0.8, gt=0, le=1)
    per_token_depth_warn: int = Field(default=100, ge=1)
    paused_auth_warn_seconds: int = Field(default=600, ge=60)
    token_fetch_fail_rate: float = Field(default=0.10, gt=0, le=1)
    callback_fail_rate: float = Field(default=0.05, gt=0, le=1)
    callback_backlog_seconds: int = Field(default=600, ge=60)
    fencing_reject_warn_seconds: int = Field(default=600, ge=60)
    worker_util_low: float = Field(default=0.50, ge=0, le=1)
    worker_util_high: float = Field(default=0.90, ge=0, le=1)
    ops_webhook: str | None = None          # 运维告警群 webhook

class BackupSettings(BaseModel):
    staging_dir: Path = Path("data/backups")
    keep_count: int = Field(default=3, ge=1)
    remote_dest: str | None = None          # rsync 目标（user@host:path 或本地路径）
    min_free_bytes: int = Field(default=4 * 1024**3, ge=0)
```

`Settings` gains fields `slo: SloSettings`, `monitor: MonitorSettings`, `backup: BackupSettings`, plus `release_root: Path | None = None`（生产= `/opt/aitrace/current`；None 时跳过 DB_COMPAT / qoder 二进制检查）。

---

### Task 1: 结构化日志 + 进程内指标采集 + 任务流水阶段事件

M4 的可观测性底座：JSON 结构化日志（含掩码）、`MetricsRegistry` 进程内指标、调度状态迁移处发射 `task_stage` 事件。

**Files:**
- Create: `src/aitrace/observability/__init__.py`
- Create: `src/aitrace/observability/metrics.py`
- Create: `src/aitrace/observability/logging.py`
- Create: `src/aitrace/observability/pipeline.py`
- Modify: `src/aitrace/core/settings.py`（追加 SloSettings/MonitorSettings/BackupSettings 与 `release_root`，`_FIELD_LABELS`/`_SECTION_MODELS` 同步）
- Modify: `src/aitrace/persistence/repositories/queue.py`（状态迁移点调 `record_stage_event`；经可选注入 `registry: MetricsRegistry | None = None`，None 时不发）
- Modify: `src/aitrace/main.py`（`configure_logging` 根配置）
- Test: `tests/unit/test_observability.py`（metrics/logging/pipeline）、`tests/unit/test_settings_ops.py`

**Interfaces:**
- Produces: `MetricsRegistry`（counter/histogram/gauge/window_rate/snapshot）、`configure_logging`、`log_event`、`record_stage_event`、Settings 新域（Task 2–5 消费）。

- [ ] **Step 1: 写失败测试（TDD RED）**

```python
# tests/unit/test_observability.py（骨架）
def test_histogram_percentiles() -> None:
    h = Histogram(buckets_seconds=(1.0, 2.0, 5.0, 10.0, 30.0, 60.0, 300.0, 900.0))
    for v in [0.5, 1.2, 8.0, 20.0, 100.0, 100.0, 400.0, 1000.0]:
        h.observe(v)
    s = h.snapshot()
    assert s.count == 8
    assert s.p50 == 60.0       # 位置 4 落入 60.0 桶上界
    assert s.p95 == 900.0

def test_window_rate_decays() -> None:
    reg = MetricsRegistry()
    c = reg.window_rate("x", window_seconds=60)
    c.inc(10)
    assert reg.snapshot()["x:rate_60s"] == 10

def test_record_stage_event_emits_metrics_and_log(caplog) -> None:
    reg = MetricsRegistry()
    with caplog.at_level("INFO"):
        record_stage_event(reg, task_type="case_ai", queue_key="u1",
                           from_state="READY", to_state="AI_RUNNING", duration_seconds=12.5)
    h = reg._histograms["task_stage_duration_seconds"].snapshot()
    assert h.count == 1
    assert "task_stage" in caplog.text and "duration_seconds" in caplog.text

def test_log_event_masks_token_values(caplog) -> None:
    with caplog.at_level("INFO"):
        log_event("ai", "token_fetched", level="INFO", token="secret-token", batch_id="B1")
    assert "secret-token" not in caplog.text

def test_slo_settings_defaults() -> None:
    s = SloSettings(); assert s.ai_p95_baseline_seconds == 360
    m = MonitorSettings(); assert m.check_interval_seconds == 60.0
```

Run: `pytest tests/unit/test_observability.py tests/unit/test_settings_ops.py -v` — 期望 FAIL（模块不存在）。

- [ ] **Step 2: 最小实现**

`metrics.py`：`Histogram` 用排序数组存值，`observe` 插入并重置缓存分位；`snapshot()` 用 `statistics.quantiles`（无外部依赖）计算 p50/p95/p99；`window_rate` 用 60s 桶列表 + 惰性修剪。`logging.py`：`logging.Formatter` 输出单行 JSON（`ts/level/component/event/msg` + 额外字段），递归遍历 `record.args` 与字段名，命中 `_SECRET_RE`（复用 `api/errors.py` 的同款正则语义：`token|secret|password|authorization|credential|api_key` 大小写不敏感）即替换 `"<已脱敏>"`；`configure_logging` 安装到 root logger，`log_event` 组装 extras。`pipeline.py` 依 Cross-Task 签名。settings 追加三个 Settings 模型并入 `Settings`（`_FIELD_LABELS` 加中文标签）。

`queue.py` 状态迁移点（`enqueue`/`claim_clean`/`claim_ai`/`finish`/release 回置等每处 `row.updated_at = now` 的地方）注入 `_metrics` 可选参数，默认 None；迁移时 `t0 = row.updated_at`（迁移前值），`t1 = now`，调 `record_stage_event`。

- [ ] **Step 3: 跑测试至绿**

Run: `pytest tests/unit/test_observability.py tests/unit/test_settings_ops.py -v` — 期望全 PASS。再跑 `make test-unit` 验证无回归。

- [ ] **Step 4: 提交**

```bash
git add src/aitrace/observability src/aitrace/core/settings.py src/aitrace/persistence/repositories/queue.py src/aitrace/main.py tests/unit/test_observability.py tests/unit/test_settings_ops.py
git commit -m "feat(m4): 结构化JSON日志+掩码、进程内指标采集与任务流水阶段事件底座"
```

---

### Task 2: 磁盘水位三级管理 + QUEUE_FULL/DISK_FULL 受理背压

投递入库前的两级队列约束与磁盘水位检查；L2/L1 清理器按 §5.3 淘汰优先级工作；超水位仍不足时拒收 `DISK_FULL(507)`、队列超深拒收 `QUEUE_FULL(429)`，均中文自解释 + `retry_after=60`。

**Files:**
- Create: `src/aitrace/storage/__init__.py`
- Create: `src/aitrace/storage/watermark.py`（见 Cross-Task 签名；依赖既有 L1/L2 路径约定：`data_dir/evidence/{session_id}/round_{n}/`、`data_dir/rawlogs/…`，按 `SessionModel`+`Round` 检索待清理实体）
- Modify: `src/aitrace/ingest/service.py`（`deliver()` 增加注入 `watermark: Callable[[], None] | None = None` 与 `depth_check: Callable[[], None] | None = None`，None=不检查保持 M0–M3 测试不变；违规抛 `AitraceError`）
- Modify: `src/aitrace/api/routes/deliveries.py`（组装预检闭包：全局深度 `count(task_queue where queue_state in (PENDING,CLEANING,READY,AI_RUNNING,WAITING)) >= global_depth` → `QUEUE_FULL`；`watermark.level_for >= FORCE 且 evict(force=True) 后仍 FORCE` → `DISK_FULL`；全检查挂在 debug 级任务流水事件）
- Modify: `src/aitrace/core/errors.py`（无需改：AitraceError 已支撑 retryable/retry_after）
- Test: `tests/unit/test_watermark.py`、`tests/integration/test_backpressure.py`

**Interfaces:**
- Consumes: `Settings.storage` 扩展字段（水位比例/TTL），`MetricsRegistry`（可选埋点 `delivery_reject`）。
- Produces: `WatermarkLevel`/`disk_fraction`/`level_for`/`evict`/`EvictionReport`（Task 3/4/7 消费）。

- [ ] **Step 1: 写失败测试（TDD RED）**

```python
# tests/unit/test_watermark.py
def test_level_for_boundaries() -> None:
    assert level_for(0.69, 0.70, 0.85) is WatermarkLevel.OK
    assert level_for(0.70, 0.70, 0.85) is WatermarkLevel.WARN
    assert level_for(0.85, 0.70, 0.85) is WatermarkLevel.FORCE

def test_disk_fraction_uses_quota_when_set(tmp_path) -> None:
    data = tmp_path / "data"; data.mkdir()
    assert disk_fraction(data, data / "l2", quota_bytes=None) >= 0.0

def test_evict_priority_order(db_session, test_settings, tmp_path) -> None:
    # 造 4 类 L1/L2 entity：超TTL、终态含未评价轮次、无效轮次、有效轮次
    report = evict(db_session, test_settings, force=True, clock=..., now=...)
    assert report.by_reason 的键顺序依淘汰优先级排列
    # 断言越早优先级目录被删（os.path.exists 为 False）

# tests/integration/test_backpressure.py
def test_deliver_rejects_when_global_depth_exceeded(client, db_session, ...) -> None:
    # 预置 global_depth 个 PENDING 任务；POST /api/v1/aitrace/tasks
    resp = ...; assert resp.status_code == 429
    body = resp.json(); assert body["code"] == "QUEUE_FULL" and body["retryable"] is True
    assert body["retry_after"] == 60 and "全局队列深度" in body["message"]

def test_deliver_rejects_when_watermark_force_and_evict_insufficient(...) -> None:
    resp = ...; assert resp.status_code == 507
    assert resp.json()["code"] == "DISK_FULL"
```

Run: `pytest tests/unit/test_watermark.py tests/integration/test_backpressure.py -v` — 期望 FAIL。

- [ ] **Step 2: 最小实现**

`watermark.py`：`disk_fraction` = l2 配额占比（quota_bytes 设置时 `used/quota`；否则 `shutil.disk_usage(data_dir)` 整盘已用比例）与整盘比例取大者；`evict` 四级顺序 = 超 L2/L1 TTL（按 Round.finished_at 或 SessionModel.created_at + ttl_days）→ 批终态且含未评价轮次（batch_state 终态 + round_status in (AWAITING_FEEDBACK,)）→ 批终态且本轮 feedback=INVALID → 批终态且 VALID；只删文件（`shutil.rmtree` 对应 evidence/round 与 rawlogs 目录），`EvictionReport` 记录 freed_bytes/removed_dirs/by_reason。清理失败继续下一项，异常汇总入日志。

`deliveries.py` 预检闭包在 `IngestService.deliver()` 前执行（同一请求事务外，用独立 session 读计数，防长事务）；请求层 dep 注入 `pre_checks: list[Callable[[], None]]` 可选参数，默认空（保持既有测试契约不变）。闭包内部用 `AitraceError(code, 中文消息含当前值/上限, http_status=429/507, retryable=True, retry_after=60)`。

- [ ] **Step 3: 跑测试至绿 + 正式路由联通验证**

Run: 两个新测试文件全绿后跑 `make test-unit`、`make test-integration`。再手动（落地前一步）以 e2e 脚本验证 429/507 实际响应形态（记录到执行记录）。

- [ ] **Step 4: 提交**

```bash
git add src/aitrace/storage src/aitrace/ingest/service.py src/aitrace/api/routes/deliveries.py tests/unit/test_watermark.py tests/integration/test_backpressure.py
git commit -m "feat(m4): L2三级水位与清理器、QUEUE_FULL/DISK_FULL受理背压"
```

---

### Task 3: 监控快照与看板⑥实时监控台扩展

`/api/v1/aitrace/monitor` 新端点（服务令牌保护）+ `dashboard/summary` 响应新增 `monitoring` 段（additive，契约导出保持 token-free）；快照组装为纯函数，供告警巡检复用。

**Files:**
- Create: `src/aitrace/observability/monitor.py`（`build_monitor_snapshot`）
- Modify: `src/aitrace/api/schemas.py`（`DashboardSummaryResponse` 增加 `monitoring: dict[str, Any] | None = None`；新 `MonitorSnapshotResponse`）
- Modify: `src/aitrace/api/routes/dashboard.py`（summary 聚合追加 `monitoring` 段）
- Create: `src/aitrace/api/routes/monitor.py`（GET `/aitrace/monitor`，`require_service_token`）
- Modify: `src/aitrace/api/app.py`（注册 monitor router；`app.state.metrics = MetricsRegistry()`；dispatcher/worker 构建处把 metrics 注入 `QueueRepository` 调用点与水位检查点）
- Modify: `src/aitrace/persistence/repositories/queue.py`（`finish` 成功/失败路径埋点 `task_finish_total{state,error}`、`fencing_reject_total`）
- Modify: `src/aitrace/ai/token_client.py`、`src/aitrace/callback/worker.py`、`src/aitrace/notify/worker.py`、`src/aitrace/knowledge/materialize.py`、`src/aitrace/probe/client.py`（各外部边界埋点：`token_fetch_ok/fail`、`callback_fail/drop/backlog`、`memory_materialize_fail`、`probe_ok/fail`、`probe_age`；均以可选 `registry: MetricsRegistry | None` 注入，默认不埋点）
- Test: `tests/unit/test_monitor.py`、`tests/integration/test_monitor_api.py`（含契约新增字段断言）

**Interfaces:**
- Consumes: Task 1 metrics/registry、Task 2 watermark、既有 DB 模型。
- Produces: `build_monitor_snapshot`（Task 4 消费）、`GET /api/v1/aitrace/monitor` 与 `monitoring` 段（Task 8 perf 报告消费）。

- [ ] **Step 1: 写失败测试（TDD RED）**

```python
# tests/unit/test_monitor.py
def test_snapshot_contains_required_sections(db_session, registry, test_settings) -> None:
    snap = build_monitor_snapshot(db_session, registry, test_settings, now=...)
    for key in ("generated_at", "runtime", "waits", "fairness", "alerts_eligible",
                "watermark", "callbacks", "workers", "audit"):
        assert key in snap
    assert snap["watermark"]["level"] in ("OK", "WARN", "FORCE")

def test_ready_wait_computed_from_updated_at(db_session, registry, test_settings) -> None:
    # 预置一个 READY task（updated_at = now - 130s）
    snap = build_monitor_snapshot(db_session, registry, test_settings, now=...)
    waits = snap["waits"]  # {"bucket_0": {...}, ...} 按 queue_key 前序深度分桶
    # 该任务等待 130s 出现在对应 queue_key 的等待列表

# tests/integration/test_monitor_api.py
def test_monitor_endpoint_guarded(client) -> None:
    assert client.get("/api/v1/aitrace/monitor").status_code in (401, 403)

def test_monitor_endpoint_with_service_token(client, auth_headers) -> None:
    resp = client.get("/api/v1/aitrace/monitor", headers=auth_headers)
    assert resp.status_code == 200
    assert "waits" in resp.json()
```

Run: `pytest tests/unit/test_monitor.py tests/integration/test_monitor_api.py -v` — 期望 FAIL。

- [ ] **Step 2: 最小实现**

`build_monitor_snapshot`：一次 DB 查询抓当前 active tasks（按其 `queue_state` / `updated_at` / `queue_key` / `task_type`）、`UserQueue`、callback 计数（`callback_status != DONE` 与 `updated_at` 差）、Report 完成数；等待时长 = `now - updated_at`（迁移时 updated_at 即进入时刻，单写者保证）；按 queue_key 的前序深度（该 key 当前 READY 前序计数）分桶 0/1-2/3-4/>4；fairness = 同桶内各 queue_key P95 等待差；workers = AI 槽位占用（`AI_RUNNING` 数/W）；audit = `AuditService.verify_chain` 结果（快照时不落审计行）。返回 dict 全中文无敏感值。

DTO 与 dashboard 扩展：`DashboardSummaryResponse.monitoring: dict | None`（旧客户端不感知 additive）；monitor router 直接返回 `MonitorSnapshotResponse(**snapshot)`。

- [ ] **Step 3: 跑测试至绿**

Run: `pytest tests/unit/test_monitor.py tests/integration/test_monitor_api.py -v` PASS 后 `make contract`（openapi 导出含新端点且 token-free check 通过）、`make test-unit`。

- [ ] **Step 4: 提交**

```bash
git add src/aitrace/observability/monitor.py src/aitrace/api/schemas.py src/aitrace/api/routes/dashboard.py src/aitrace/api/routes/monitor.py src/aitrace/api/app.py src/aitrace/persistence/repositories/queue.py src/aitrace/ai/token_client.py src/aitrace/callback/worker.py src/aitrace/notify/worker.py src/aitrace/knowledge/materialize.py src/aitrace/probe/client.py tests/unit/test_monitor.py tests/integration/test_monitor_api.py
git commit -m "feat(m4): 监控快照与看板\u2465实时监控台 endpoints + 全边界埋点"
```

---

### Task 4: 告警判定与运维告警群触达

纯函数阈值判定（可测）+ 巡检线程（60s）+ 钉钉运维告警群 webhook 投递；审计哈希链校验失败立即 CRITICAL 并暂停相关写入（校验任务在巡检内执行 `verify_chain`，失败置 `app.state.audit_failed=True`，`AuditService.append` 前置检查拒绝——需要 `AuditService` 增加 `blocked` 标志注入，默认 False 保持既有测试）。

**Files:**
- Create: `src/aitrace/observability/alerts.py`
- Modify: `src/aitrace/api/app.py`（monitor 后台线程挂在 lifespan，`settings.monitor.enabled` 时启动；`app.state.audit_failed`）
- Modify: `src/aitrace/core/audit.py`（`append()` 接受注入 `blocked: Callable[[], bool] | None = None`，True 时抛 `AitraceError("AUDIT_CHAIN_BROKEN", ..., http_status=503)` 中文提示；`verify_chain` 结果缓存 60s）
- Test: `tests/unit/test_alerts.py`、`tests/unit/test_audit_blocked.py`

**Interfaces:**
- Consumes: Task 3 `build_monitor_snapshot`、Settings.monitor 阈值。
- Produces: `Alert`/`evaluate_alerts`/`send_alerts`（Task 8 报告与告警流消费）。

- [ ] **Step 1: 写失败测试（TDD RED）**

```python
# tests/unit/test_alerts.py
def test_no_alerts_when_healthy(snapshot_ok, test_settings) -> None:
    assert evaluate_alerts(snapshot_ok, test_settings, now=...) == []

def test_paused_auth_sustained_triggers_warn(snapshot, test_settings) -> None:
    snapshot["runtime"]["paused_auth"]["count"] = 1
    snapshot["runtime"]["paused_auth"]["sustained_seconds"] = 660
    alerts = evaluate_alerts(snapshot, test_settings, now=...)
    assert any(a.name == "paused_auth_sustained" and a.severity == "WARN" for a in alerts)

def test_audit_chain_broken_is_critical(snapshot, test_settings) -> None:
    snapshot["audit"]["verified"] = False
    alerts = evaluate_alerts(snapshot, test_settings, now=...)
    assert any(a.name == "audit_chain_broken" and a.severity == "CRITICAL" for a in alerts)

def test_send_alerts_posts_to_ops_webhook(caplog) -> None:
    posted: list[str] = []
    send_alerts([Alert(name="n", severity="WARN", threshold="t", current="c", message="中文")],
                ops_webhook="https://ops.example/hook", poster=lambda w, text: posted.append(w))
    assert posted == ["https://ops.example/hook"]

def test_audit_append_blocked_when_chain_broken(db_session) -> None:
    svc = AuditService(...); svc.verify_chain = lambda: False   # 模拟断裂
    with pytest.raises(AitraceError) as ei:
        svc.append(actor="t", action="x", target="y", detail={}, blocked=...)
    assert ei.value.code == "AUDIT_CHAIN_BROKEN"
```

Run: `pytest tests/unit/test_alerts.py tests/unit/test_audit_blocked.py -v` — 期望 FAIL。

- [ ] **Step 2: 最小实现**

`evaluate_alerts` 覆盖 spec §7.3 可本地判定项（队列深度全局/单令牌、PAUSED_AUTH 持续、令牌拉取失败率、callback 失败率/积压、fencing 持续、水位 WARN/FORCE、审计链、memory 物化失败、worker util 低/高、WAITING 超时转换数）；每条 Alert：`threshold` 输出「要求值（当前值 中文单位）」。`send_alerts`：文本格式 `【AITrace告警】{severity} {name}：{message}（阈值 {threshold}，当前 {current}）`，poster 单次投递，异常仅 WARN 日志（不重试风暴）。巡检线程在 `run_tick` 同生命周期处启动：每次 sleep 周期内跑快照+evaluate+send；线程异常仅记 ERROR 不退出。

`audit.py`: `append(..., blocked: Callable[[], bool] | None = None)`,默认 None；`verify_chain` 结果缓存（`_last_verify_at/_last_verify_ok`，60s 内复用）。

- [ ] **Step 3: 跑测试至绿**

Run: 两个测试文件全绿；再 `make test-unit`，重点确认 `tests/unit/test_audit.py` 既有断言未被破坏。

- [ ] **Step 4: 提交**

```bash
git add src/aitrace/observability/alerts.py src/aitrace/api/app.py src/aitrace/core/audit.py tests/unit/test_alerts.py tests/unit/test_audit_blocked.py
git commit -m "feat(m4): 告警判定与运维告警群触达、审计链断裂熔断写入"
```

---

### Task 5: /readyz + 启动自检五查 + 优雅排水

启动 fail-fast 五查（配置/目录与水位/DB revision 兼容/运维二进制/Secret 引用）+ 凭据文件扫描；`/readyz` 反映 ready/stopping 语义；lifespan 关闭先转 not-ready 再排水。

**Files:**
- Create: `src/aitrace/ops/__init__.py`
- Create: `src/aitrace/ops/startup.py`
- Modify: `src/aitrace/main.py`（`configure_logging` 后调 `run_startup_checks`，失败打印中文清单 `exit 1`）
- Modify: `src/aitrace/api/routes/health.py`（`GET /readyz` 读 `app.state.ready/stopping`）
- Modify: `src/aitrace/api/app.py`（lifespan 进入前 `app.state.ready=True`；关闭顺序：`ready=False; stopping=True` → 停 loop → engine.dispose；`create_app` 接受可选 `startup_checks: list[Callable[[], None]] | None` 便于测试）
- Modify: `deploy/scripts/healthcheck.sh`（改探 `/api/v1/readyz`）
- Test: `tests/unit/test_startup.py`、`tests/unit/test_readyz.py`

**Interfaces:**
- Consumes: Settings（`release_root`、storage 水位比例）、既有 alembic revision（`alembic heads` 经 venv 或 `sqlalchemy` 查 `alembic_version` 表）。
- Produces: `run_startup_checks`（Task 9 ops 目标消费）、`/readyz`（Task 6 upgrade 轮询消费）。

- [ ] **Step 1: 写失败测试（TDD RED）**

```python
# tests/unit/test_startup.py
def test_missing_data_dir_rejected(tmp_path, session_factory) -> None:
    settings = test_settings_with(data_dir=tmp_path/"nope", release_root=None)
    with pytest.raises(StartupError) as ei:
        run_startup_checks(settings, session_factory)
    assert "数据根目录" in str(ei.value) and "建议" in str(ei.value)

def test_credential_like_file_in_runtime_rejected(tmp_path, session_factory) -> None:
    data = tmp_path/"data"; (data/"runtime").mkdir(parents=True)
    (data/"runtime"/"qoder-auth.json").write_text('{"token":"x"}')
    with pytest.raises(StartupError) as ei:
        run_startup_checks(settings_with(data), session_factory)
    assert "qoder-auth.json" in str(ei.value)  # 文件名可见、内容不回显

def test_dev_mode_skips_release_checks(session_factory) -> None:
    run_startup_checks(settings_with(release_root=None), session_factory)  # 不抛

def test_readyz_returns_503_when_stopping(test_app) -> None:
    test_app.state.stopping = True
    resp = test_app.get("/api/v1/readyz")
    assert resp.status_code == 503
```

Run: `pytest tests/unit/test_startup.py tests/unit/test_readyz.py -v` — 期望 FAIL。

- [ ] **Step 2: 最小实现**

`startup.py` 五查：① 配置/画像已在 load_settings + load_profiles（重复校验幂等）；② `data_dir`（evidence/rawlogs/runtime/memory/logs 子目录自创建校验）+ `HOME` 存在可写 + `shutil.disk_usage` 水位 < FORCE 或（=FORCE 时）仍可清理：直接调用 Task 2 `disk_fraction/level_for`；③ DB 可连 + `alembic_version` 单行存在，`release_root` 设置时读 `REVISION` 与 `DB_COMPAT` 文件（上界 revision 比较：领先进上界才拒绝，允许领先/落后按 expand 窗口规则——落后于 head 拒绝）；④ `release_root/bin/qoder` 存在且可执行（None 时跳过）；⑤ Secret：`service_token`/`aitest.read_token` 已由 load_settings 解析（此处断言非 None）。附 `scan_for_credential_files`（runtime + HOME 递归，文件名模式 `*token*|*credential*|*.pem|id_rsa*|qoder*.json` 或内容含 `BEGIN PRIVATE KEY` → 命中文件列表）。`StartupError(RuntimeError)` 消息中文逐条 + 「建议」修复行。

`/readyz`：`app.state.ready and not app.state.stopping and engine SELECT 1` → 200 `{"status":"ready","version":...}`；否则 503 `{"status":"not_ready","reason":"stopping/db_unavailable"}`。

- [ ] **Step 3: 跑测试至绿**

Run: 新测试全绿后 `make test-unit` + `make lint`（新增模块 mypy 覆盖）。实际启动一次 `make run`（dev）验证没误报（记录证据）。

- [ ] **Step 4: 提交**

```bash
git add src/aitrace/ops/startup.py src/aitrace/main.py src/aitrace/api/routes/health.py src/aitrace/api/app.py deploy/scripts/healthcheck.sh tests/unit/test_startup.py tests/unit/test_readyz.py
git commit -m "feat(m4): /readyz 与启动自检五查、凭据扫描、优雅排水语义"
```

---

### Task 6: 备份/恢复 CLI + systemd 定时单元

`aitrace-backup` Python CLI：预检保留策略清理 → `pg_dump -Fc` → `pg_restore --list` 校验 + SHA-256 digest → 异盘推送；空间不足跳过（专用码 3）并告警；`restore` 子命令用于 RPO/RTO 演练；systemd `aitrace-backup.service/timer`（`OnCalendar=daily`）。

**Files:**
- Create: `src/aitrace/ops/backup.py`（含 `console_main(argv) -> int` 与 `run_backup/run_restore`，console entry `aitrace-backup = aitrace.ops.backup:console_main`）
- Modify: `pyproject.toml`（`[project.scripts]` 注册；`[project]` 无需新依赖——pg_dump 走 subprocess）
- Create: `deploy/systemd/aitrace-backup.service`、`deploy/systemd/aitrace-backup.timer`
- Modify: `deploy/env/aitrace.env.example`（BackupSettings 相关非敏感项注释示例；凭据禁止写入）
- Test: `tests/unit/test_backup.py`、`tests/ops/test_backup_drill.py`（真实 pg_dump 到 tmp 目录 + list 校验 + digest；需 `TEST_DATABASE_URL` 门禁与 `pg_dump` 在 PATH）

**Interfaces:**
- Consumes: Settings.backup（staging/keep/remote/min_free）、settings.database.dsn。
- Produces: `run_backup`/`run_restore`（Task 9 ops 演练脚本消费）、timer 单元（Task 9 资产测试扩充）。

- [ ] **Step 1: 写失败测试（TDD RED）**

```python
# tests/unit/test_backup.py
def test_dsn_split_keeps_password_out_of_argv() -> None:
    dsn = "postgresql+psycopg://aitrace:s3cret@127.0.0.1:5432/aitrace"
    dbname, env = _pg_env_parts(dsn)
    assert "s3cret" not in dbname and env["PGPASSWORD"] == "s3cret"
    # env 只进子进程环境，永不进 argv/日志

def test_backup_skips_when_space_short(tmp_path, ...) -> None:
    settings = backup_settings_with(staging=tmp_path, min_free_bytes=10**13)  # 不可能满足
    with pytest.raises(SpaceShortageError):
        run_backup(settings, staging=tmp_path/"backups", keep=3, clock=...)

def test_retention_removes_oldest(tmp_path, ...) -> None:
    # 造 5 个 dump 文件；keep=3 → 保留最新 3 个 + digest 同步删除
```

Run: `pytest tests/unit/test_backup.py -v` — 期望 FAIL；`make lint` 首跑报未注册 entry（记录形态）。

- [ ] **Step 2: 最小实现**

`backup.py`：`_pg_env_parts(dsn)` 用 `sqlalchemy.engine.url.make_url` 拆 host/port/user/password/dbname → `pg_dump -Fc --dbname=postgresql://...`（密码经子进程 env `PGPASSWORD`，DSN 打印走 `repr` 已脱敏）；`run_backup`：staging 递归创建（data_dir 下 `backups/`，独立于水位统计）→ `shutil.disk_usage(staging)` 预检 `min_free_bytes`（不足抛 `SpaceShortageError` exit 3 + ALERT 日志行，不产生 dump）→ 保留策略（`keep_count`，含 `.digest` 同删）→ dump → `pg_restore --list` 校验（非 0 抛 `BackupError` 中文）→ sha256 digest 落盘 → remote_dest 推送（`rsync -a` 或 `shutil.copy2` 当本地路径；推送失败告警但 dump 保留）。`run_restore`：`pg_restore --clean --if-exists -d <dsn>`（演练用测试库，默认拒绝恢复非空生产库：`--force` 才允许）。`console_main`：argparse 子命令 backup/restore + 中文错误输出 + 退出码 0/1/3。

systemd：`aitrace-backup.service`（Type=oneshot、User=aitrace、`ExecStart=/opt/aitrace/current/venv/bin/aitrace-backup backup`、EnvironmentFile=/etc/aitrace/aitrace.env、ReadWritePaths=/var/lib/aitrace /var/log/aitrace）+ `aitrace-backup.timer`（`OnCalendar=*-*-* 03:00:00`、`Persistent=true`、`Unit=aitrace-backup.service`）。

- [ ] **Step 3: 跑测试至绿**

Run: 单元全绿；对真实测试库执行一次 `aitrace-backup backup --staging /tmp/aitrace-backup-drill --keep 2` 与 `restore --force` 到测试库（记录证据：dump 大小、digest、pg_restore 成功、恢复后 `make test-integration` 全绿）。

- [ ] **Step 4: 提交**

```bash
git add src/aitrace/ops/backup.py pyproject.toml deploy/systemd/aitrace-backup.service deploy/systemd/aitrace-backup.timer deploy/env/aitrace.env.example tests/unit/test_backup.py tests/ops/test_backup_drill.py
git commit -m "feat(m4): aitrace-backup CLI、digest校验、异盘推送与systemd定时单元"
```

---

### Task 7: 发布脚本补全（六步升级/回滚/保留3个release）

`upgrade.sh` 严格六步 + `/readyz` 轮询 + 失败回滚 + 保留 3 release；`rollback.sh` 停服切回校验；`install_release.sh` 不再自行切 `current`；脚本支持 `AITRACE_INSTALL_ROOT` 与 `AITRACE_SERVICE_NAME` 环境覆盖（可测性）；全脚本中文错误、bash -n 通过。

**Files:**
- Modify: `deploy/scripts/install_release.sh`（去掉 `ln -sfn current` 与 systemctl 操作，只装 release + venv + wheel + 生成 `alembic.ini`）、`deploy/scripts/upgrade.sh`（全重写）、`deploy/scripts/rollback.sh`（重写）、`deploy/scripts/healthcheck.sh`（已改 readyz，本任务补 `--retry` 轮询参数）
- Test: `tests/ops/test_release_scripts.py`（构造 fake release 树 + fake systemctl/readlink 存根 PATH，驱动 upgrade/rollback 全路径断言调用序列与切换结果）

**Interfaces:**
- Consumes: Task 5 `/readyz`；`deploy/scripts/migrate.sh` 既有签名 `migrate.sh <release-dir>`（本任务加 `--release` 别名不变破）。
- Produces: upgrade/rollback 六步脚本（Task 9 发行/回滚烟测 + M5 gray release 消费）。

- [ ] **Step 1: 写失败测试（TDD RED）**

```python
# tests/ops/test_release_scripts.py（骨架）
def test_upgrade_happy_path_executes_six_phases(tmp_path, fake_scripts) -> None:
    # fake：versions v1(旧 current)/v2(新)；fake systemctl 记 stop/start 顺序；
    # fake healthcheck 返回 ready。跑 upgrade.sh --release .../v2 --skip-backup-check
    log = run_upgrade(...)
    assert "预检通过" in log and "迁移完成" in log and "readyz 通过" in log
    assert (root/"current").resolve() == root/"releases"/"v2"
    assert justify(calls) == ["stop", "migrate", "symlink", "start"]  # 顺序断言

def test_upgrade_rolls_back_on_readyz_failure(tmp_path, fake_scripts) -> None:
    fake healthcheck 第一次 fail；断言 current 回到 v1 且 start 调用 2 次

def test_retention_keeps_three_releases(tmp_path, fake_scripts) -> None:
    预置 v1..v5；upgrade 后仅剩 v3/v4/v5
```

Run: `pytest tests/ops/test_release_scripts.py -v` — 期望 FAIL（脚本无 `--skip-backup-check` 等语义）。

- [ ] **Step 2: 最小实现**

`upgrade.sh` 骨架：

```bash
#!/usr/bin/env bash
set -euo pipefail
ROOT="${AITRACE_INSTALL_ROOT:-/opt/aitrace}"
release="${1:?usage: upgrade.sh <release-dir>}"
# 0) 预检：目录/REVISION/DB_COMPAT 存在、磁盘可用、当前 /readyz、备份完成(digest 今日或 --skip-backup-check)
# 1) install_release.sh（不动 current）
# 2) systemctl stop aitrace && 等待优雅退出（TimeoutStopSec 内，确认 /readyz 不可达）
# 3) migrate.sh ${ROOT}/releases/${rev}
# 4) ln -sfn 新 current && systemctl start && 轮询 /readyz（60×2s）
# 5) 失败：rollback.sh ${previous}；exit 1
# 6) cleanup：保留最近 3 个 release + current
```

`rollback.sh`：stop → ln → start → readyz 轮询（120s）→ 失败 exit 1 与中文提示。所有脚本日志同步输出到 stderr + `/var/log/aitrace/upgrade.log`（存在写权限时）。`install_release.sh` 接受 `--make-current` 可选参数（首次安装用）。

- [ ] **Step 3: 跑测试至绿**

Run: `pytest tests/ops/test_release_scripts.py -v` 全绿；`bash -n` 全部脚本；`make lint`。

- [ ] **Step 4: 提交**

```bash
git add deploy/scripts tests/ops/test_release_scripts.py
git commit -m "feat(m4): upgrade/rollback 六步发布（readyz轮询、失败回滚、保留3个release）"
```

---

### Task 8: SLO 标定 harness（`make perf`）

`tools/perf_harness.py`：复用 tests/e2e 的进程管理方式（app + mock 子进程）跑 fake 链批量投递，采集受理/清洗/AI/令牌队列分桶等待/公平性/背压/回调风暴指标，输出 `perf_report.json` + 对照 §7.1 各目标列 PASS/FAIL 摘要；纯统计函数单测先行。

**Files:**
- Create: `tools/perf_harness.py`（标准库为主 + httpx；参数 `--batches`、`--ai-workers`、`--out`、`--profile fairness|burndown|backpressure`
  ；内嵌 `percentile`/`bucket_waits`/`fairness_gap`/`summarize` 纯函数）
- Create: `tests/perf/test_perf_math.py`（纯函数单测）
- Modify: `Makefile`（`perf:` 目标：`TEST_DATABASE_URL=... python tools/perf_harness.py --profile all`；`ops:` 与 `test-security:` 见 Task 9）

**Interfaces:**
- Consumes: Task 3 的 `task_stage` 指标与 wait 快照、Task 2 背压端点；e2e 既有起服模式；`tests/fixtures/ai_fake_scenario.json`。
- Produces: `perf_report.json` + `report/slo-calibration.md`（Task 9 回填 §8.2 基线）。

- [ ] **Step 1: 写失败测试（TDD RED）**

```python
# tests/perf/test_perf_math.py
def test_percentile_nearest_rank() -> None:
    from tools.perf_harness import percentile  # 通过 sys.path 注入 tools
    assert percentile([1, 2, 3, 4], 95) == 4
    assert percentile([1, 2, 3, 4, 5], 50) == 3

def test_bucket_waits_by_predecessor_depth() -> None:
    waits = [("u1", 0, 10.0), ("u1", 1, 30.0), ("u2", 0, 5.0)]
    buckets = bucket_waits(waits)   # list[(queue_key, depth, wait_seconds)] → {"0": {...}}
    assert buckets["0"]["p95"] >= 5.0

def test_fairness_gap_within_bucket() -> None:
    gap = fairness_gap({("u1", "0"): [10, 20], ("u2", "0"): [5, 7]}, bucket="0")
    assert gap == 13.0

def test_summarize_against_slo_targets() -> None:
    results = summarize(perf_metrics_dict, SloSettings())  # 返回 [(指标, 目标, 实测, ok)]
    assert all(isinstance(r.ok, bool) for r in results)
```

Run: `pytest tests/perf/test_perf_math.py -v` — 期望 FAIL（模块不存在）。

- [ ] **Step 2: 最小实现**

`perf_harness.py`：纯函数块（`percentile` 最邻近等级法、`bucket_waits` 按前序深度 0/1-2/3-4/>4、`fairness_gap` 同桶内 max(P95)−min(P95)、`summarize` 对照 `SloSettings`）；采集块：拉起 app（TEST_DATABASE_URL + fake AI + W=参数）+ mock aitest；profile=burndown：顺序投递 20 批 × 3 failed cases（batch_id 唯一化 U 后缀防幂等碰撞，投递前 TRUNCATE 或随机 product 域），轮询 dashboard/monitor 直至全部终态，从 registry 日志（task_stage 事件）聚合各阶段耗时；profile=backpressure：一次性投递 > global_depth 任务，统计 429 `QUEUE_FULL` 数量与 retry_after；profile=fairness：多 queue_key 交错投递，聚合分桶 P95 差；回调风暴：全链完成后查 mock `/__mock/callbacks` 数量与乱序丢弃。结束统一落 `perf_report.json`（含各 profile 指标、SLO 对照表、ucha 结论）并打印中文摘要。`make perf` 默认 `--profile all --batches 20`（本地 gate 用 `--batches 8` 快跑）。

- [ ] **Step 3: 跑测试至绿 + 实跑一次快配标定**

Run: 纯函数测试全绿；`TEST_DATABASE_URL=... .venv/bin/python tools/perf_harness.py --profile all --batches 8` 实跑（记录证据：时长、报告路径、各 Profile 关键数、SLO 对照结果）；`make lint` 覆盖 tools 下新文件（确认 mypy 与 ruff 配置外不检查 tools——若未含则记录裁决：perf 工具不参与 lint 门禁但测试必须绿）。

- [ ] **Step 4: 提交**

```bash
git add tools/perf_harness.py tests/perf Makefile
git commit -m "feat(m4): make perf 标定harness（分桶等待/公平性/背压/回调风暴 profiles）"
```

---

### Task 9: `make ops`/`make test-security` 目标、CI 接线、SLO 回填与收尾

补齐 Makefile 目标与 CI，把 Task 1–8 的验证固化为可重复门禁；用 Task 8 实测基线回填 spec §7.1/§8.2；程序计划与 README 回写；最终 `make verify` + `make test-security` + `make ops` +（快配）`make perf` 全绿后提交推送。

**Files:**
- Modify: `Makefile`（`test-security:` = `pytest tests/unit -k "token or probe or audit or whitelist or credential or startup or backup"`；`ops:` = `pytest tests/ops` + backup drill smoke；`verify` 保持既有四步不扩——发布前人工跑 security/ops/perf，理由：perf 依赖计时环境不适合每次 merge；CI 单独加 security/ops 步）
- Modify: `.github/workflows/ci.yml`（verify job 后加 `make test-security`、`make ops`；perf 不进 CI）
- Modify: `docs/superpowers/specs/2026-11-25-aitrace-design-v1-final.md`（§7.1 标注「M4 标定基线（2026-11-27 实测）」表与限定词；§8.2 调度参数收敛值：C=4 保持、W 按实测（2/4）、D、15/45/60）；附录 B 追加 M4 实施修订（08–10 条）
- Modify: `docs/superpowers/plans/2026-11-25-aitrace-v1-program.md`（M4 行更新为 DONE + 证据；M4 计划链接）
- Modify: `README.md`（M4 状态段 + perf/ops 命令说明）
- Test: 无新代码测试（文档任务）；依赖 Task 1–8 全绿证据。

- [ ] **Step 1: 更新 Makefile 与 CI（先红后绿——新增目标首跑必须可用）**

```bash
make test-security   # 期望：收集到 security 相关测试并全绿
make ops             # 期望：tests/ops 资产+演练全绿（含 Task 6 drill）
```

- [ ] **Step 2: 回填 SLO 基线与文档**

以 `perf_report.json`（Task 8 快配实测）逐项填入 §7.1 表「实测（P95）」列即可，**不虚构数字**：只写实测值与「未达标项」标注；§8.2 回填 C/W/D 收敛值（若实测 W=2 更稳则记 W=2 并附依据）；附录 B 追加「V1.2 实施修订（2026-11-27，M4 SLO/Ops）」条目（监控双载体/水位背压/readyz 与自检/备份 CLI/timer/六步发布/perf 基线/ops 门禁）。

- [ ] **Step 3: M4 里程碑门禁全绿**

停常驻演示服务（门禁纪律）→ `make verify` → `make test-security` → `make ops` → `make perf`（快配）→ `make lint`；全部证据记录本计划文末「执行记录」。

- [ ] **Step 4: 提交推送（用户已授权阶段 push）**

```bash
git add Makefile .github/workflows/ci.yml docs/superpowers/specs/2026-11-25-aitrace-design-v1-final.md docs/superpowers/plans/2026-11-25-aitrace-v1-program.md README.md docs/superpowers/plans/2026-11-27-aitrace-m4-slo-ops.md
git commit -m "docs(m4): M4 SLO/Ops 收尾——perf/ops/security门禁、CI接线、SLO基线回填与计划状态"
git push
```

---

## 验收清单（M4 Definition of Done）

- [ ] `make verify` 全绿（lint / 冒烟测试含单测全量 / contract / e2e）；`make test-security`、`make ops`、`make perf`（快配）全绿且证据留档。
- [ ] 结构化 JSON 日志与任务流水阶段事件可用一条 trace_id 全链检索；日志/告警/审计均不含令牌类字段。
- [ ] 受理侧三级约束（全局队列深度 / 单令牌深度 / L2 水位）实测触发 429 `QUEUE_FULL`/507 `DISK_FULL`（中文错误含当前值/上限/retry_after）。
- [ ] `/readyz` 200/503 语义、启动自检五查、优雅排水 smoke 通过；健康状况在 `upgrade.sh` 轮询中被消费。
- [ ] 备份→校验→恢复演练完成：dump 生成、digest 落盘、`pg_restore` 成功、恢复后集成测试全绿。
- [ ] `upgrade.sh` 六步顺序与失败回滚经 fake 测试断言；release 保留 3 个。
- [ ] 标定报告 `report/slo-calibration.md` 回填 spec §7.1/§8.2，无虚构数字。
- [ ] 程序计划 Execution Status M4 行 = DONE + 证据；本计划文末「执行记录」完整。

## 执行记录（2026-11-27）

| 任务 | 证据 |
| --- | --- |
| T1 观测底座 | commit 44f2ded；`make verify` 全绿（当时 362 通过）；tests/unit/test_observability.py 15 用例含令牌掩码、直方图最近秩、阶段事件双落（指标+日志） |
| T2 水位+背压 | commit（见本轮提交）；tests/unit/test_watermark.py 8 用例 + tests/integration/test_backpressure.py 4 用例全绿；`make verify`：368 passed + 26 contract + 4 e2e，coverage 94.98%；实测 429 QUEUE_FULL（全局深度 5、单令牌深度 1，retry_after=60）、507 DISK_FULL（quota 1000B/占用 900B）；WARN 档（70%）触发仅清 TTL、FORCE 档按 未评价→无效→有效 顺序淘汰且不碰 RUNNING 批次 |

裁决记录（T2）：
- `IngestService.deliver` 增 `pre_checks: list[Callable[[], None]] | None`，背压闭包在路由层构建（含 levels 顺序：全局深度→单令牌深度→磁盘水位），不侵入 ingest 内部逻辑。
- evict 只删数据目录（evidence round 目录/rawlogs 批次目录），DB 行保留作审计；evidence 父会话目录删空后一并 rmdir。
- 无 `l2_quota_bytes` 时按 data_dir 所在卷物理使用率计提；data_dir 不存在视为 0；rawlogs 目录 mtime 统一按 UTC 感知时间与 now 比较（修 naive/aware TypeError）。
- 生产 ORM 同批 flush 的 round/session 插入排序不可依赖（生产链路本就先写 session 再写 round），测试种子显式 flush 顺序对齐。