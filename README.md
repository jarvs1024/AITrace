# 知因（AITrace）

> 自动化失败用例 AI 分析进化系统

AITrace 与现有自动化测试平台 **aitest** 形成协同矩阵：aitest 负责自动化任务调度与执行，知因负责失败用例的**智能追踪、根因定界与知识自进化**。业务聚焦 **SSD 与 CXL 硬件**自动化测试场景。

![知因（AITrace）概要架构图](architecture-v1.png)

## 文档

| 文档 | 内容 |
|---|---|
| [docs/superpowers/specs/2026-11-25-aitrace-design-v1-final.md](docs/superpowers/specs/2026-11-25-aitrace-design-v1-final.md) | **V1.2 定稿（唯一有效方案）**：系统定位 · 双向接口契约 · 三阶段管线 · 并行调度 · 数据模型 · 安全与 SLO · Linux 进程部署 · 工程目录结构 · 评审记录 · Qoder CLI SDK 参考 |
| [docs/superpowers/specs/2026-11-22-aitrace-design-v1.md](docs/superpowers/specs/2026-11-22-aitrace-design-v1.md) | 历史存档（已被 2026-11-25 终稿取代，仅作追溯） |
| [docs/superpowers/plans/2026-11-25-aitrace-v1-program.md](docs/superpowers/plans/2026-11-25-aitrace-v1-program.md) | V1 总体实施计划（M0–M5 边界、接口、验证矩阵） |
| [docs/superpowers/plans/2026-11-25-aitrace-m0-foundation.md](docs/superpowers/plans/2026-11-25-aitrace-m0-foundation.md) | M0 契约冻结与骨架详细 TDD 计划 |
| [docs/superpowers/plans/2026-11-26-aitrace-m1-cleaning.md](docs/superpowers/plans/2026-11-26-aitrace-m1-cleaning.md) | M1 清洗流水线详细 TDD 计划（含执行记录） |
| [docs/superpowers/plans/2026-11-26-aitrace-m2-ai.md](docs/superpowers/plans/2026-11-26-aitrace-m2-ai.md) | M2 AI 分析链详细 TDD 计划（含执行记录与全部裁决） |
| [docs/superpowers/plans/2026-11-27-aitrace-m3-feedback.md](docs/superpowers/plans/2026-11-27-aitrace-m3-feedback.md) | M3 反馈闭环里程碑详细 TDD 计划（含执行记录与全部裁决） |
| [docs/superpowers/plans/2026-11-27-aitrace-m4-slo-ops.md](docs/superpowers/plans/2026-11-27-aitrace-m4-slo-ops.md) | M4 SLO/Ops 里程碑详细 TDD 计划（含执行记录与全部裁决） |

## 架构图

| 文件 | 规格 | 用途 |
|---|---|---|
| [architecture-v1.svg](architecture-v1.svg) | 1920 × 1090 矢量 | **概要图（深色主题）· 唯一可编辑图源** |
| [architecture-v1.png](architecture-v1.png) | 3840 × 2180（2× 渲染） | 文档内嵌与评审投屏 |
| [architecture-v1.html](architecture-v1.html) | 内嵌 SVG 的查看器 | 浏览器中直接打开浏览 |
| [architecture-v1-detail.svg](architecture-v1-detail.svg) | 1820 × 1164 矢量 | 详版：补充状态机、接口契约与安全红线 |
| [architecture-v1-detail.png](architecture-v1-detail.png) | 3640 × 2328（2× 渲染） | 详版位图 |
| [architecture-v1-detail.html](architecture-v1-detail.html) | 内嵌 SVG 的查看器 | 详版查看器 |
| [mermaid 原图](docs/superpowers/specs/2026-11-22-aitrace-design-v1-mermaid.png) | 1024 × 572 | 原始方案配图，仅作历史留档 |

## 核心架构原则

1. **系统独立解耦**——知因独立部署，与 aitest 通过标准双向 API 完成任务投递、进度同步、结果回写与人工反馈闭环，对测试执行引擎零侵入。
2. **高保真三段脱水 + AI 全权定界**——后端只做确定性加工（多源日志清洗、三段式切片、跨源时间轴对齐、保守指纹去重）；设备型号 / FW / 槽位等硬件信息以 aitest 权威下发为准，日志与只读探针仅作核对；根因分类与深度定界全权交由 Qoder AI。
3. **多任务并行与用户令牌隔离**——「谁启动任务、用谁的 Qoder 令牌分析」，一令牌一条串行队列、队列间轮转公平；执行隔离单元为 `task_id`（runtime / 凭证 / 探针 / 知识库均按任务隔离）。
4. **两态极简反馈闭环**——钉钉群只做轻量告警，证据核验与反馈交互全部收口 aitest 详情页；反馈仅【有效 / 无效（可附带修正）】两态。

## 流水线三阶段

| 阶段 | 职责 | 关键机制 |
|---|---|---|
| **阶段一** 分源日志清洗、三段切片与时间对齐 | 把几十至数百 MB 的四路原始日志提纯为高信噪比联合上下文 | 四源专用脱水策略 · 前端基线 / 首错时间窗（四路取最早）/ 末尾终态三段切片 · dmesg 与串口时间锚校准 · 保守指纹去重（批内 · 保留计数） |
| **阶段二** AI 全局分类与深度定界（Qoder SDK + 令牌级串行队列 + 共享 Worker 池） | 采用 aitest 权威硬件信息、归类根因、构建可回溯证据链 | 清洗与 AI 两级调度 · 每用户令牌队列串行 1 · 派发时 JIT 取令牌 · 主 Agent 批次全局统筹 · GitLab 源码核对 · 子 Agent 沙箱大日志深挖 · SSH 只读硬件探针 · Skills SOP 与 Memory 共享底座 |
| **阶段三** 通知触达、两态进化与运营看板 | 触达工程师并驱动知识自进化 | 钉钉精简告警 · VALID 写入案例库并刷新领域热记忆 · INVALID+修正蒸馏教训条目 · INVALID 无修正隔离丢弃 · 六大运营视图 |

## 安全红线

- **令牌安全**：`qoder_access_token` 全程 HTTPS 传输，仅在任务执行期间驻留内存，严禁写入数据库、任务队列、缓存或任何应用日志。
- **SDK 认证**：只允许内存 `accessToken(token)`；禁止 `accessTokenFromEnv()`、本地 CLI 登录态，任务 HOME 使用 tmpfs 且不持久化。
- **硬件探查安全**：SSH 实时探针严格受限于只读命令白名单，严禁写盘、`nvme format`、控制器 reset 或固件刷写等变更操作。

## 仓库状态

**M0 基础骨架 + M1 清洗流水线 + M2 AI 分析链 + M3 反馈闭环 + M4 SLO/Ops 已实现并验证**（2026-11-27）：M0 契约冻结、数据模型与审计链、队列/租约/令牌槽门、投递与查询 API、aitest mock（含控制台前端）；M1 四源脱水插件、首错定位、跨源对齐与锚点级联、三段切片、双层指纹、L1 证据落盘、截流五规则、清洗 Worker、聚合闸；M2 每令牌串行队列与 JIT 令牌（无兜底、仅内存持有）、Qoder 主/子代理编排（国内 SDK qodercn_agent_sdk，VPC 网关 + 精确大小写模型名 `GLM-5.3-Flash`）、七维置信度评分、截流/暂停/恢复/放弃生命周期、PROGRESS/RESULT/QUEUE_STATE 回调通道、批次关账、后台调度循环（daemon 线程七段 tick）；M3 两态反馈与知识闭环（VALID 贡献案例库/投影/撤回、INVALID+修正蒸馏教训条目带观察期、引用级联 `CITING_STALE`、热记忆物化 worker + 预筛注入）、aitest 日志拉取接入清洗（清单/Range 下载/失败时刻截断）、重诊与附件上传、运营看板聚合 API、钉钉通知 worker（单事件聚合卡片 batch_card/final/negated、高危卡嵌套、指数退避降级、GET 通知配置 + TTL 缓存）、SSH 只读探针（双栏白名单交集 + 运行/拒绝/超时三态审计）。验证结果（2026-11-27 fresh）：`make verify` 全绿（`make lint` 清洁 · `make test` 341 passed（cov 95.05%）· `make contract` 26 passed · `make e2e` 4 passed）；系统级 fake 链真实 HTTP 走查「投递 → COMPLETED(FW_DEFECT, 0.78) → 反馈 VALID → 案例库/热记忆物化 → 看板 case_count 0→1」，NOTIFY 三卡（诊断/高危 AER/终态 COMPLETED）经 mock `/__mock/cards` 捕获；aitest mock 控制台与看板新数据 Playwright 浏览器实测通过（截图/快照取证)。此前里程碑关键验证：M2 cn 真实模型正向全链 COMPLETED（真实 CN PAT + twsc VPC 专线 + GLM-5.3-Flash + 双子代理 → RESULT 回调 + 状态 API 完整报告，OTHER 硬顶置信度 0.5 生效），真实 SDK 负向全链 FAILED 正确传播；M2 修复轮收敛 3 个状态语义缺陷 + CN 网关三要素 + 看板乱序聚合（详见 M2 计划文末「端到端验证记录」）。M4 SLO/Ops（2026-11-27 收尾）：结构化 JSON 日志 + 任务流水阶段事件（一条 trace 全链检索、令牌掩码）；受理侧三级背压（全局/单令牌队列深度 + L2 三级水位 → 429 `QUEUE_FULL` / 507 `DISK_FULL` 中文自解释错误）；监控快照与看板⑥实时监控台；告警判定与运维告警群触达；`/readyz` + 启动自检五查 + 优雅排水；备份/恢复 CLI（pg_dump + SHA-256 digest + 保留策略 + 真实恢复演练）与 systemd timer；六步发布脚本（readyz 轮询、失败自动回滚、release 保留 3 个）；`make perf` 标定 harness。M4 门禁全绿（2026-11-27 实测）：`make verify`（lint 清洁 · 445 passed / 2 skipped（cov 94.43%）· contract 26 · e2e 4）· `make test-security` 41 passed · `make ops` 20 passed（含真实 pg_dump/pg_restore 演练）· `make perf` 快配 burndown 8/8、fairness 11/11、背压 20 投递/10 受理/10×429；SLO 基线已回填 spec §7.1「实测（P95）」列与 §8.2（C=4、W=2、D=360s），附录 B 追加 M4 实施修订。M5（加固）按总体计划继续实施。实现期目录按终稿 §10.3 组织：`src/aitrace`（生产代码）· `contracts`（双向契约）· `mocks/aitest`（独立 aitest mock）· `deploy`（部署与配置）· `data`（运行数据）· `tests`（分层测试）。生产环境为 **Linux + systemd 进程部署（无容器）**：单主进程、本机 PostgreSQL、TLS 反向代理、随 release 锁定的 Qoder CLI runtime，发布与回滚按 §8.5 执行。

## 验证与运维命令

| 命令 | 内容 | 使用时机 |
|---|---|---|
| `make verify` | lint + test + contract + e2e | 每次 merge 前必跑（CI 同款） |
| `make test-security` | 安全相关单测集（token / probe / audit / whitelist / credential / startup / backup 关键词） | 发布前人工跑；CI 在 verify job 后接线 |
| `make ops` | tests/ops 资产检查 + 真实备份恢复演练（`AITRACE_OPS_DRILL=1` 显式开启，需 `TEST_DATABASE_URL` 与 pg_dump/pg_restore 在 PATH） | 发布前人工跑；CI 在 verify job 后接线 |
| `make perf` | 标定 harness 全 profile（默认 20 批，输出 `perf_report.json` 并对照 §7.1 SLO 打 PASS/FAIL 摘要） | 本地 gate；快配直跑 `tools/perf_harness.py --profile all --batches 8`；不进 CI（计时环境不适合每次 merge） |
| `make lint` | ruff check + ruff format --check + mypy（仅 src/aitrace） | 每次提交前 |

发布前完整门禁顺序：停常驻演示服务 → `make verify` → `make test-security` → `make ops` → `make perf`（快配）→ `make lint`。
