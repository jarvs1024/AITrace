# M3 Feedback 实施计划（通知 · 两态反馈进化 · 双端看板 · 会话轮次与重诊 · 日志拉取 · SSH 探针）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 完成阶段三：通知触达、两态反馈驱动的知识进化闭环、双端运营看板、会话/轮次/重诊模型、aitest 日志拉取与 SSH 只读探针，使全链路「投递 → 清洗 → AI → 报告 → 回调 → 反馈 → 知识进化 → 看板」闭环可用。

**Architecture:** 在 M2 单进程基础上新增：反馈 API（贡献记账 + 投影重算 + 引用级联）、`memory_materialize` / `notify` 两类独立任务消费者（与 AI 令牌队列同库不同队列，`run_tick` 顺序驱动）、重诊/附件 API、看板聚合查询、aitest LogClient 接入清洗缺源拉取、SSH ProbeClient（白名单交集 + 逐次审计）。

**Tech Stack:** 同上 M2：Python 3.13 / FastAPI / Pydantic v2 / SQLAlchemy 2.0 / PostgreSQL 16 / httpx（MockTransport 注入测试）/ pytest。

**Spec:** [docs/superpowers/specs/2026-11-25-aitrace-design-v1-final.md](../specs/2026-11-25-aitrace-design-v1-final.md) §4.3 / §4.4 / §4.1.4-4.1.6 / §6.1 / §6.3 / §6.5.3

## Global Constraints（承接 v1-program，逐字）

- `qoder_access_token` 仅任务执行期内存持有，绝不落库/队列/缓存/日志；本里程碑不新增任何令牌落盘路径。
- SSH 探针只读：全局命令白名单 ∩ 画像白名单，禁止写/格式化/复位/固件刷新；每次运行审计。
- 反馈与看板数据资产、口径唯一在知因；aitest 只拉不推。
- 通知链路失败降级绝不阻塞分析主链路。
- 根因五类闭集：`TEST_ENV_ERROR` / `FW_DEFECT` / `SCRIPT_ISSUE` / `HW_DEFECT` / `OTHER`。
- 全部时间戳 UTC ISO-8601；错误信息自解释（中文字段名、当前值、要求）。
- 每任务 red → green；测试断言逐字保留；`make verify` 为合并门禁。
- 提交仅在阶段边界（本里程碑结束），推送仅在用户明确要求后。

## File Structure

| 文件 | 责任 |
|---|---|
| `src/aitrace/api/routes/feedback.py`（新） | `POST /aitrace/feedback` 两态评价 |
| `src/aitrace/api/routes/session_ops.py`（新） | rediagnose / attachments / 会话详情（rounds/report） |
| `src/aitrace/api/routes/dashboard.py`（新） | `GET /aitrace/dashboard/summary` 六视图聚合 |
| `src/aitrace/api/schemas.py`（改） | 新响应模型（FeedbackResponse/RediagnoseResponse/DashboardSummary…） |
| `src/aitrace/api/app.py`（改） | 挂载三个新 router |
| `src/aitrace/knowledge/__init__.py`、`knowledge/service.py`（新） | 反馈知识闭环：贡献记账/投影重算/撤回/级联/热文件刷新（纯 SQLAlchemy，无 HTTP） |
| `src/aitrace/knowledge/materialize.py`（新） | MemoryMaterializeWorker：Top N 热文件 temp+rename、memory_hot_file 表 |
| `src/aitrace/knowledge/prescreen.py`（新） | 清洗后按稳定指纹预筛：use_hits +1、参考块、维度⑥观察期计分 |
| `src/aitrace/notify/__init__.py`、`src/aitrace/notify/worker.py`（新） | NotifyWorker：notify-config 拉取缓存、聚合卡片、降级 |
| `src/aitrace/ingest/log_client.py`（新） | aitest 日志清单/下载（Range、failed_at_utc 截断、路径白名单） |
| `src/aitrace/cleaning/worker.py`（改） | 缺源时经 LogClient 拉取（可注入 `log_loader` 协议，默认 None=本地直读） |
| `src/aitrace/probe/client.py`（新） | ProbeClient：白名单 ∩ + 审计 + 逐次快照 |
| `src/aitrace/probe/whitelist.py`（新） | 全局白名单解析（settings + 画像 probe_commands 交集） |
| `src/aitrace/runtime/dispatcher.py`（改） | run_tick 挂 Notify/Materialize worker；构造 ProbeClient |
| `src/aitrace/persistence/repositories/queue.py`（改） | `claim_generic(owner, now, task_type)` 供 notify/materialize 消费 |
| `mocks/aitest/src/aitest_mock/main.py`（改） | `/logs`（list+下载）、`/tasks/{batch_id}`、`/notify-config`、`/notify/webhook`（卡片捕获） |
| `tests/contract/test_aitest_mock.py`（改） | mock 新端点契约 |
| `tests/integration/test_feedback_knowledge.py`（新） | 反馈→贡献→投影→撤回→级联 |
| `tests/integration/test_session_ops.py`（新） | 重诊/附件/详情 |
| `tests/integration/test_dashboard_api.py`（新） | 看板聚合口径 |
| `tests/unit/test_log_client.py`（新） | 下载/Range/截断/越界 |
| `tests/unit/test_notify.py`（新） | 聚合卡片/降级 |
| `tests/unit/test_probe.py`（新） | 白名单/拒绝写命令/审计 |
| `tests/integration/test_memory_materialize.py`（新） | 热文件生成与预筛注入 |

---

### Task 1: mock aitest 扩展（日志/通知配置/批次环境 + 契约）

**Files:** 改 `mocks/aitest/src/aitest_mock/main.py`；测 `tests/contract/test_aitest_mock.py`；docs `mocks/aitest/README.md`

**Interfaces:**
- Produces:
  - `GET /api/v1/aitest/logs?batch_id=&case_id=` → `{"items": [{"path": "var/log/...", "size_bytes": 123, "modified_at_utc": 1795...}]}`（场景文件 `scenarios/default.yaml` 声明 `logs` 段，mock 从 `mocks/aitest/logs/` 目录静态文件生成清单）
  - `GET /api/v1/aitest/logs?path=...&failed_at_utc=...` → 200 全量 / 206 `Content-Range` / 416 / 404；`path` 必须落在 `mocks/aitest/logs/` 白名单内，拒绝 `..` 穿越（403）；`failed_at_utc` 提供时按行截断到该时刻之前的行
  - `GET /api/v1/aitest/tasks/{batch_id}` → `{"batch_id":..., "env_topology": {...}, "git_context": {...}}`
  - `GET /api/v1/aitest/notify-config?product_id=` → `{"webhook": "http://127.0.0.1:{mock}/api/v1/aitest/notify/webhook", "at_users": ["zhangsan"], "high_risk_webhook": null}`（webhook 指回 mock 自身实现捕获）
  - `POST /api/v1/aitest/notify/webhook`（不需要读令牌）→ 记录卡片到 `_cards`，返回 `{"ok": true}`；`GET /__mock/cards` 返回 `{"items": [...卡片...]}`
- 依赖：现有 `_auth`、`MockApiError`、`_SCENARIO` 加载。

- [ ] **Step 1: 契约测试失败先行**：在 `tests/contract/test_aitest_mock.py` 追加 4 个用例（要点）：
  ```python
  def test_log_list_requires_token(): ...  # 无 X-Aitest-Read-Token → 403 FORBIDDEN
  def test_log_download_full_and_range():
      # 全量 200；Range: bytes=0-9 → 206 且 Content-Range 正确；Range 越界 → 416
  def test_log_download_rejects_traversal():
      # path="../../etc/passwd" → 403 PATH_FORBIDDEN
  def test_notify_config_and_webhook_capture():
      # notify-config 返回 webhook 指向 mock；POST webhook 后 /__mock/cards 长度 +1
  ```
- [ ] **Step 2: 跑红**：`pytest tests/contract/test_aitest_mock.py -q` 期望新用例失败。
- [ ] **Step 3: 实现**：mock `main.py` 按上文 Interfaces 加端点；`logs/` 目录放 2 个样例文件（内容 ≥ 2 行用于截断断言）。
- [ ] **Step 4: 跑绿**：`pytest tests/contract -q` 全绿。
- [ ] **Step 5: 提交**：本里程碑任务合并最终一次提交（阶段边界），中间不单独 commit。

### Task 2: 知因 LogClient + 清洗缺源拉取

**Files:** 新 `src/aitrace/ingest/log_client.py`；改 `src/aitrace/cleaning/worker.py`；测 `tests/unit/test_log_client.py`、`tests/integration/test_cleaning_log_pull.py`

**Interfaces:**
- Consumes: settings.aitest（base_url/read_token/timeout_seconds）；`CleaningWorker.__init__` 增可选 `log_client: LogClient | None = None`。
- Produces:
  ```python
  class LogDescriptor:  # dataclass
      path: str; size_bytes: int
  class LogClient:
      def __init__(self, *, base_url: str, read_token: str, timeout_seconds: float = 30.0,
                   transport: httpx.BaseTransport | None = None) -> None: ...
      def list_logs(self, *, batch_id: str, case_id: str) -> list[LogDescriptor]: ...
      def download_to(self, *, path: str, dest: Path, failed_at_utc: int | None = None,
                      max_bytes: int = 512 * 1024 * 1024) -> int: ...
      # 返回落盘字节数；astest 4xx 抛 LogFetchError(code, retryable)；Range 分段
      # 64KB 步进 + 续传；failed_at_utc 提供的行截断由服务端做，客户端按 206 流式拼装
  ```
- CleaningWorker（`process_one`）：`raw_root` 下的声明路径缺失时，若 `self._log_client` 非 None 则 `download_to` 拉取；单源 404 → 该源标注缺口（`outcome` 已有缺口语义，拉取失败仅记录 summary）继续；全部源不可得且本地也缺失 → `_fail`。

- [ ] **Step 1: 单元测试先行**（MockTransport 响应 200/206/404/416；断言目标文件内容、Range 头、穿越路径拒绝在客户端同样校验 `..` absent）。
- [ ] **Step 2: 跑红**。
- [ ] **Step 3: 实现** LogClient；`CleaningWorker` 注入点 + 拉取逻辑。
- [ ] **Step 4: 集成测试**：假 aitest httpx 服务（或 MockTransport 注入 CleaningWorker 构造的 LogClient），投递声明路径本地缺失 → 清洗后 rawlogs 出现拉取文件且 pipeline 正常。
- [ ] **Step 5: 跑绿**（`pytest tests/unit/test_log_client.py tests/integration/test_cleaning_log_pull.py -q`）。

### Task 3: 两态反馈 API 与知识闭环（核心）

**Files:** 新 `src/aitrace/knowledge/service.py`、`src/aitrace/api/routes/feedback.py`；改 `src/aitrace/api/schemas.py`、`src/aitrace/api/app.py`；测 `tests/integration/test_feedback_knowledge.py`

**Interfaces:**
- Consumes: `SessionModel.stable_fingerprint/domain_id`、`Round.round_id/round_status/feedback/confidence`、`Report.result/evidence/citation`、`CaseLibrary/CaseContribution` 表、`QueueRepository.enqueue`（MEMORY_MATERIALIZE，queue_key=domain_id）。
- Produces:
  ```python
  class KnowledgeService:
      def __init__(self, session: Session, ids, clock) -> None: ...
      def apply_feedback(self, *, session_id: str, batch_id: str, case_id: str,
                         reviewer: str, action: Literal["VALID", "INVALID"],
                         correction: dict | None, queue: QueueRepository) -> FeedbackOutcome: ...
  FeedbackOutcome = dataclass(feedback_id, round_no, version, applied: bool, cascaded: int)
  ```
- 语义（spec §4.3.2 / §4.4.5 / §6.5.3，逐条）：
  1. `(session_id, round_no)` 幂等键；缺省 round_no = 会话最新轮次；`batch_id/case_id` 冗余校验不一致 → `FEEDBACK_CONFLICT` 400。
  2. 轮次非 COMPLETED/终态不可评 → 409；无报告不可评 VALID。
  3. 同评审人同 action 重复提交 → 返回既有结果 `applied=False`，不重复计数/级联。
  4. 版本变更（不同评价内容或不同评审人）：同一事务撤回上一版本贡献（`status=withdrawn`）→ 按剩余 active 贡献重算条目投影（`category/ai_conclusion/correction/evidence_summary/confirm_count/observation_hits/entry_status`）→ 写新贡献（VALID→`kind=valid`；INVALID+correction→`kind=lesson`；INVALID 无修正→只撤回不新增）。
  5. 仅当指纹无其他 active 贡献 → `entry_status=demoted`；观察期：最新 active lesson 的 `observation_hits<5` → `entry_status=observing`。
  6. 聚合闸级联：轮次报告 `citation.source_session` 非空且本轮被 INVALID 否定 → 引用同源会话的全部报告 `citing_status=CITING_STALE`（只改引用注记 result 区块，不重跑 AI），返回 `cascaded` 计数，并 commit 后为每案例 enqueue NOTIFY（追加「本批 N 例引用的结论已被否定」）。
  7. 每次有效贡献同步摘存 L1 证据摘要：`evidence_summary = result.evidence[] 的 excerpt 拼接（截断 4096）`。
  8. 写贡献事务中顺带 upsert case_library（同指纹合并、`exact_fingerprints` 追加去重、`use_hits` 不清零）。
  9. 事务提交后 enqueue MEMORY_MATERIALIZE（每 domain 一条，已存在 PENDING 则跳过）。
- API：`POST /api/v1/aitrace/feedback`，Pydantic 模型校验根因闭集枚举；响应 `{feedback_id, session_id, round_no, version, status:"applied"|"idempotent"}`；错误全中文。

- [ ] **Step 1: 集成测试先行**（造会话+轮次+报告：VALID→贡献/条目投影/confirm_count；同人重复→幂等；INVALID+correction→lesson/observing；改评→撤回+重算；INVALID 无修正→只统计不写 Memory；引用级联→同组 CITING_STALE；批级 case_id='' 不写案例库）。
- [ ] **Step 2: 跑红**。
- [ ] **Step 3: 实现** KnowledgeService + feedback router。
- [ ] **Step 4: 跑绿**。
- [ ] **Step 5: `pytest tests/integration -q` 回归**。

### Task 4: 热记忆物化 + 预筛注入

**Files:** 新 `src/aitrace/knowledge/materialize.py`、`src/aitrace/knowledge/prescreen.py`；改 `src/aitrace/runtime/dispatcher.py`、`src/aitrace/persistence/repositories/queue.py`、`src/aitrace/ai/worker.py`、`src/aitrace/ai/prompts.py`；测 `tests/integration/test_memory_materialize.py`

**Interfaces:**
- Consumes: `CaseLibrary`（entry_status ∈ {active, lesson, observing}，demoted 不进）、`MemoryHotFile`、画像 `memory_namespace`（`profiles/resolver.py` 已解析，`ResolvedProfile.memory_namespace`）、`AIWorker._build_ctx`。
- Produces:
  ```python
  class MemoryMaterializeWorker:
      def __init__(self, queue: QueueRepository, hot_dir: Path, top_n: int = 200) -> None: ...
      def run_once(self, session, *, data_dir: Path, owner: str, now) -> int:
          # claim_generic(memory_materialize) → 查该 domain 案例（active/lesson/observing，
          # 按 use_hits DESC, updated_at DESC 取 top_n）→ temp 文件写 JSON → os.replace
          # → upsert memory_hot_file（entries/version+1/generated_at）→ finish(DONE)
      def refresh_domain(self, session, domain_id: str, *, data_dir: Path, now) -> str:
          # 纯函数式：返回落盘文件路径（供 prescreen/测试复用）
      def hot_file_path(self, data_dir: Path, domain_id: str) -> Path:
          # data_dir / "memory" / f"{memory_namespace_of_domain}.json"
  def prescreen_case_library(session, *, domain_id: str, stable_fingerprint: str,
                             round_id: str, now) -> ReferenceHit | None:
      # 命中：use_hits+1（同轮同指纹只 +1，靠 round 的去重标记）、返回
      # ReferenceHit(category, correction, conclusion, observing: bool, hits: int)
  ```
- AIWorker `_build_ctx`：清洗完成后调 prescreen（迟到注入不用阻塞）——命中时把参考块写进主 Agent 提示词 `<user_hint>` 内（「工程师提示（仅作排查方向…）」）；`_lookup_reference`（现有）与本预筛合并口径：报告 `result` 增加 `reference` 注释与维度⑥评分（observing → 只作参考不加分=0.5 中性，过观察期 lesson=1.0，valid=0.7）。
- run_tick 顺序：清洗 → 路由 → AI → …… → `MemoryMaterializeWorker.run_once`（独立队列，AI 前后皆可，放回调后）。

- [ ] **Step 1: 集成测试先行**（构造 3 条案例（含 demoted 1 条）→ enqueue MEMORY_MATERIALIZE → run_once → 热文件 top 序正确、demoted 排除、memory_hot_file 表版本 +1；prescreen 命中 → use_hits+1、observing 判定；AIWorker fake 链命中注入后报告维度⑥分正确）。
- [ ] **Step 2: 跑红**。
- [ ] **Step 3: 实现**。
- [ ] **Step 4: 跑绿 + 全量回归**。

### Task 5: 重诊 / 附件 / 会话详情 API

**Files:** 新 `src/aitrace/api/routes/session_ops.py`；改 `src/aitrace/api/schemas.py`、`src/aitrace/api/app.py`、`src/aitrace/api/routes/sessions.py`（详情扩展）；测 `tests/integration/test_session_ops.py`

**Interfaces:**
- Consumes: IngestService 的建会话/入队路径（重诊复用 `case_clean` 入队）、`Attachment` 表、`Round/Report`。
- Produces:
  - `POST /api/v1/aitrace/sessions/{session_id}/rediagnose`：body `{trigger_user_id, hint?: str}` → 校验会话终态（最新轮次 COMPLETED/FAILED/AWAITING_FEEDBACK 可重诊；RUNNING 409）→ 新 `Round(round_no+1, trigger_user_id=操作人, queue_key=操作人)` + `enqueue(case_clean, payload 含 {hint, attach_paths:[…, uploads/*]})` → 响应 `{session_id, round_no, round_id, task_id}`。线索进 `<user_hint>`（清洗/AI 提示词组装时注入），`hint` 不覆盖硬证据。
  - `POST /api/v1/aitrace/sessions/{session_id}/attachments`：multipart `file` + `uploaded_by`；上限 `settings.storage.max_attachment_bytes`；落 `rawlogs/{batch_id}/uploads/{attachment_id}_{原名}`；sha256 记账；`Attachment` 行 `round_no=会话.latest_round_no+1` 语义（挂到下一次重诊轮次）；响应 `{attachment_id, path, size_bytes, sha256}`。
  - `GET /api/v1/aitrace/sessions/{session_id}`：扩展现有状态返回——`rounds[]`（history_rounds：round_no/诊断时间/confidence/评价状态）、`referenced_logs`、`process_steps`、`grant_feedback`（当前轮可否评价）。
- [ ] **Step 1: 集成测试先行**（重诊建新轮次+线索入 payload+queue_key=操作人；RUNNING 会话重诊 409；附件大小超限 413 中文错误；附件落盘+sha256 校验；详情含 rounds/referenced_logs）。
- [ ] **Step 2: 跑红**。
- [ ] **Step 3: 实现**。
- [ ] **Step 4: 跑绿 + 回归**。

### Task 6: 运营看板聚合 API

**Files:** 新 `src/aitrace/api/routes/dashboard.py`；改 `src/aitrace/api/schemas.py`、`src/aitrace/api/app.py`；测 `tests/integration/test_dashboard_api.py`

**Interfaces:**
- Produces: `GET /api/v1/aitrace/dashboard/summary?product_id=`（可空=全产品；运行时服务令牌鉴权同其他路由）返回：
  ```json
  {
    "generated_at": "...", "product_id": null,
    "overview": {"session_count": n, "round_count": n, "valid_rate": 0.42,
                 "round_distribution": {"1": n, "2": n, "3+": n},
                 "feedback_distribution": {"VALID": n, "INVALID": n, "AWAITING_FEEDBACK": n, "FAILED": n, "RUNNING": n}},
    "by_product": [{"product_id": "...", "session_count": 275, "round_count": 391, "valid": 117, "invalid": 11, "awaiting": 140, "failed": 1, "running": 6, "valid_rate": 0.425}],
    "root_cause_distribution": {"TEST_ENV_ERROR": n, "FW_DEFECT": n, "SCRIPT_ISSUE": n, "HW_DEFECT": n, "OTHER": n},
    "knowledge": {"case_count": n, "hot_memory_hit_rate": 0.31, "avg_confidence": 0.62,
                  "low_confidence_ratio": 0.12},
    "runtime": {"queue_counts": {"PENDING": n, "CLEANING": n, "READY": n, "AI_RUNNING": n, "WAITING": n},
                "paused_users": n},
    "confidence_histogram": {"0.0-0.3": n, "0.3-0.5": n, "0.5-0.7": n, "0.7-0.9": n, "0.9-1.0": n}
  }
  ```
- 口径（§4.3.3 ②）：诊断记录数=product 的 case 会话数（`case_id!=''`）；状态列按会话最新轮次 `round_status`；有效诊断率按会话；批级共因会话单列不计入（`overview` 内可不含，`by_product` 不含）；hot_memory_hit_rate = 命中轮次/预筛总轮次（用 `case_library.use_hits` 与完成轮次数的比值近似，注释口径）。
- [ ] **Step 1: 集成测试先行**（seed 两个产品多个会话/轮次/报告/反馈 → 断言每个区块数值与口径；product_id 过滤只含该产品）。
- [ ] **Step 2: 跑红**。
- [ ] **Step 3: 实现**（SQLAlchemy 聚合查询）。
- [ ] **Step 4: 跑绿 + 回归**。

### Task 7: 钉钉通知 worker（聚合卡片 + 降级）

**Files:** 新 `src/aitrace/notify/worker.py`、`src/aitrace/notify/cards.py`；改 `src/aitrace/runtime/dispatcher.py`、`src/aitrace/persistence/repositories/queue.py`；测 `tests/unit/test_notify.py`、`tests/integration/test_notify_worker.py`

**Interfaces:**
- Consumes: `POST /api/v1/aitest/notify-config?product_id=`（含 webhook/at_users/high_risk_webhook；httpx 客户端注入）、`Batch/Report/Round`。
- Produces:
  ```python
  class NotifyWorker:
      def __init__(self, queue: QueueRepository, config_fetcher: Callable[[str], NotifyConfig],
                   poster: Callable[[str, dict], None], *, cache_ttl: int = 300,
                   default_webhook: str | None = None, clock=None) -> None: ...
      def run_once(self, session, now, owner) -> int:
          # claim_generic(notify) → 依据 task.payload{event: batch_card|high_risk|final|negated,
          # batch_id, …} 组装/更新卡片 dict（进程内按 batch_id 聚合计数）→ poster(webhook, card)
          # config 拉取失败 → default_webhook；仍失败 → 跳过（不重试超过,max_attempts 由队列控制）
  def build_card(event, batch, counts, conclusions, detail_url) -> dict: ...  # 纯函数
  ```
- 触发点：批次关账（`batch_close` 终态汇总卡片）、报告 COMPLETED 回写后（首例卡片/高危卡片 `FW ASSERT`/`AER`/`CXL Link Down` 关键词判定）、反馈级联否定（卡片追加行）。入队均在各自业务事务后 enqueue task_type=notify，queue_key=batch_id。
- **降级原则**：任何 notify 失败只影响本 notify 任务（retry 后 FORGET），不阻塞、不回调、不波及主链路。
- [x] **Step 1: 单元测试先行**（build_card 纯函数：首例卡/计数聚合/高危独立卡/终态汇总/否定追加行；NotifyWorker 用假 fetcher/poster 断言聚合与缓存 TTL；fetcher 抛错 → default_webhook；poster 抛错 → 任务 FAILED 不抛栈）。
- [x] **Step 2: 跑红**。
- [x] **Step 3: 实现 + run_tick 挂载 + batch_close 与 AI 回写处入队**。
- [x] **Step 4: 集成测试**（DB 批次终态 → notify 任务 → 卡片内容正确）。
- [x] **Step 5: 跑绿 + 回归**（310 passed，覆盖 95.12%，make lint 全绿）。
- **实施裁决**（红线⑧回写）：
  1. `NotifyWorker.run_once` 签名裁决为 `run_once(self, session, queue, now, owner)`——QueueRepository 会话绑定，worker 常驻 app.state 时 queue 由调用方按会话注入（计划原签名 `run_once(self, session, now, owner)`）。
  2. notify payload 为单事件契约：`payload = {event, batch_id, product_id, …事件字段}`（Task #48 既有断言 `notify.payload["event"] == "negated"` 逐字锁定）；高危卡非独立事件，嵌套为 `payload["high_risk"] = {"keywords": [...], "detail": "..."}`，batch_card 处理时同任务追加第二张卡。
  3. 失败退避不走 `queue.retry()`（其对非 clean 任务置 READY 会错误暴露给 AI claim）：`_requeue_or_fail` 手动 attempts+1，<max → PENDING + `next_retry_at=30*2^(n-1)`（上限 1800s），≥max → FAILED。
  4. mock 的 notify-config 用 GET（X-Aitest-Read-Token 头），非计划 POST 猜测。
  5. `batch_close._TERMINAL` 补 "VALID"/"INVALID"——已反馈批次此前永不关账、final 卡永不发送（真实缺陷）。
  6. negated 事件额外保留 `session_id`（citing 会话）键以满足 Task #48 逐字断言 `notify.payload["session_id"] == "S-cite"`。
  7. `test_dispatcher.py::test_run_tick_drives_full_chain` 查询过滤器由 `task_type != CALLBACK` 收窄为 `== CASE_AI`——notify 任务按设计新增为每案例一行，原 `scalar_one()` 多行歧义；断言 `queue_state == DONE` 逐字保留。

### Task 8: SSH 只读探针（白名单交集 + 审计）

**Files:** 新 `src/aitrace/probe/whitelist.py`、`src/aitrace/probe/client.py`；测 `tests/unit/test_probe.py`

**Interfaces:**
- Consumes: 全局白名单（settings 新字段 `probe.global_whitelist: list[str]`，默认只读命令集：`nvme list`、`nvme id-ctrl`、`nvme smart-log`、`nvme error-log`、`cxl list`、`lspci -vvv…`、`df -h`、`ps -ef | grep …`；正则列表）、画像 `probe_commands` 交集。
- Produces:
  ```python
  class ProbeClient:
      def __init__(self, *, runner: Callable[[str, str], tuple[int, str, str]] | None = None,
                   global_whitelist: list[str], audit: AuditService,
                   profile_whitelist: list[str] | None = None) -> None: ...
      def run(self, *, host: str, command: str, session_id: str) -> ProbeResult:
          # 1) command 逐条匹配 global ∩ profile 正则 → 不匹配抛 ProbeDenied（写命令/越权）
          # 2) runner(host, command)（默认 subprocess ssh -o BatchMode=yes -o ConnectTimeout=5）
          # 3) audit.append(actor="probe", action="SSH_PROBE", target=host, detail={command, rc, bytes, session_id})
          # 4) 返回 ProbeResult(command, exit_code, stdout, stderr, at)
  ProbeDenied(ValueError): code="PROBE_FORBIDDEN"（命令不在白名单）
  ```
- 审计哈希链沿用 `AuditService.append`。超时（runner 内 timeout=10s）→ `ProbeTimeout`。
- [x] **Step 1: 安全测试先行**（白名单拒绝 `reboot`/`nvme format`/`rm`/管道注入；允许 `nvme list`；画像交集空 → 拒绝；审计落库且哈希链 verify 通过；runner 记录超时）。
- [x] **Step 2: 跑红**。
- [x] **Step 3: 实现**（settings 增 `probe` 段 + `_FIELD_LABELS` 中文标签；`deploy/config/config.dev.yaml`/`config.prod.example.yaml` 同步示例）。
- [x] **Step 4: 跑绿 + `make lint`**（341 passed，覆盖 95.05%，mypy 90 文件无问题）。
- **实施裁决**（红线⑧回写）：
  1. 画像白名单沿用既有字段 `DomainProfile.probe_whitelist`（计划文本写 probe_commands，属笔误；M2 画像模型已带只读校验 `_FORBIDDEN_PROBE`）。
  2. 交集语义实现：全局白名单为锚定正则（`re.fullmatch`，参数扩展必须在模式内显式允许）是最终安全闸；画像白名单按前缀语义（命令==条目或「条目 + 空格」开头）；两栏皆过才放行。`profile_whitelist=None` 或空列表一律视为交集为空 → `ProbeDenied`。
  3. `ProbeClient.__init__` 较计划签名增两个可选参数 `connect_timeout=5`/`command_timeout=10`（默认 SSH runner `-o BatchMode=yes -o ConnectTimeout=N` 使用）；runner 抛 `subprocess.TimeoutExpired`/`TimeoutError` → `ProbeTimeout`（code=PROBE_TIMEOUT）且审计 `SSH_PROBE_TIMEOUT`。
  4. 拒绝也逐次审计（action=SSH_PROBE_DENIED，detail 含 command/session_id/reason），哈希链 verify 不受影响。
  5. settings 增 `probe` 段（`ProbeSettings`：`global_whitelist` 默认内置只读命令集、`connect_timeout_seconds`、`command_timeout_seconds`）+ `_FIELD_LABELS`/`_SECTION_MODELS` 中文标签；两个 config 示例以注释块同步（缺省用内置白名单，注释注明「空列表 = 拒绝一切」）。
  6. 本任务产出为 lib 级组件（whitelist/client + settings + 测试），未挂接 API 路由——计划 Task 8 范围即如此；重诊入口装配留给后续里程碑。

### Task 9: 验收（门禁 + 系统级 + 浏览器 + 文档回写）

- [x] `make verify` 全绿（lint/mypy/test/contract/e2e，安全测试并入 make test）。
- [x] 系统级：fake 链服务常驻，真实 HTTP 走一遍「投递 → 完成 → 反馈 VALID → 案例库/热文件 → 看板数字变化」，NOTIFY 卡片可见（mock /__mock/cards）。
- [x] 浏览器：mock 控制台与看板新数据实测（at least 截图/snapshot 证据）。
- [x] 文档回写：spec 附录 B（M3 实施修订记录）、`docs/superpowers/plans/2026-11-25-aitrace-v1-program.md`（M3 状态行）、`README.md` 仓库状态、`mocks/aitest/README.md`。
- [x] 阶段边界 commit + push（用户已授权本里程碑「验证 ok 后提交 push」）。

## 兼容性说明（与 spec 冲突时的裁决基线）

- 引用级联只覆盖**同批同组引用注记改写**与 `CITING_STALE`；`cited_version` 语义维持 M2 现状（版本=被引用报告轮次重诊次数+1）。
- 看板 hot_memory_hit_rate 因子缺轮次级预筛日志，以 `use_hits/完成轮次数` 近似，README 口径注明（错成本：需口径修复，仅看板展示，不影响分析）。
## 执行记录（2026-11-27）

**门禁**：`make verify` 全绿 fresh——`make lint` 清洁（mypy 90 源文件）· `make test` 341 passed（cov 95.05%，含安全测试）· `make contract` 26 passed · `make e2e` 4 passed；M3 无新增 schema 迁移（沿用 0001–0005）。

**系统级 fake 链真实 HTTP 走查**（mock :8090 + AITrace :8080 双服务常驻，aitrace_test 库）：投递批次 BATCH-WALK（CASE-walk，四路日志源 data/live/logs/）→ 清洗 → 路由 → fake AI → 状态 API `stage=COMPLETED`（report `root_cause=FW_DEFECT`、`confidence=0.78`、solution「升级固件至 2.4.2」）→ 反馈 `VALID`（反馈号 F-20261127-92766717，status=applied）→ case_library 行 `entry_status=valid`（frame af1be7fcc8b16c96，confirm_count=1）→ 热记忆物化 `data/live/memory/domain_enterprise.json` → 看板 `knowledge.case_count` 0→1、`overview.feedback_distribution` VALID:1。NOTIFY 卡片经 mock `GET /__mock/cards` 捕获 9 张，其中 BATCH-WALK-0003 三卡齐全：诊断卡（「已出结论：1 例」+ CASE-walk FW_DEFECT 0.78）、高危卡（命中关键词 AER + 摘要「固件缺陷：超时路径缺少重试，触发 AER 致命错误」）、终态汇总卡（批次状态 COMPLETED + 会话分布）。回调记录 5 条（PROGRESS×4 + RESULT×1）。失败批次 BATCH-WALK-0001/0002（VALIDATION_FAILED → stage=FAILED「一键重新触发」）终态与状态 API 一致。

**浏览器实测**（Playwright）：mock 控制台与运营看板显示新数据（根因分布 FW_DEFECT:1、平均置信度 0.78、AI 完成率 25%、回调事件 PROGRESS×4/RESULT×1、会话 S-…-95959144 COMPLETED），截图 `/tmp/aitrace-walk/mock-console.png` + 无障碍快照取证。

**走查插曲与裁决**：① 门禁首跑 2 个 e2e 失败（批次 180s 超时 FAILED）——根因：走查常驻服务与 e2e 共用 aitrace_test 库抢任务（门禁与走查并跑会互踩）；停掉走查服务后复跑 2 个 e2e 34.87s 全绿、`make verify` 全绿。② 裸跑（未设 TEST_DATABASE_URL）时 `test_query_returns_queued_stage` 读到 e2e 残留的 COMPLETED 会话（IngestService 按 `(product_id, batch_id, case_id)` 幂等返回既有会话，属设计行为）——测试环境残留所致，非产品缺陷；已 TRUNCATE 测试库收尾。裁决：门禁执行前必须停掉本机常驻的 AITrace/mock 演示服务（两者共用测试库）。
