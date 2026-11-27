# 02 · 告警处置运维手册

## 1. 触达渠道与判定口径

- 告警判定：`src/aitrace/observability/alerts.py::evaluate_alerts`（本地可判定项，spec §7.3 子集已实现），投递文案格式：【AITrace告警】{severity} {name}：{message}（阈值 {threshold}，当前 {current}）。
- 触达渠道：钉钉运维告警群 webhook（`AITRACE_MONITOR__OPS_WEBHOOK`，`dingtalk_text_poster`）；任务 FAILED / 水位预警 / 审计异常 / ERROR 飙升四类场景。
- 内置实现阈值（`MonitorSettings` 默认值，均可配置）：

| 告警 | 实现阈值 | 处置动作 |
|---|---|---|
| 全局队列深度 | ≥ 上限 80%（`global_depth_warn_fraction=0.8`） | 查全局队列深度与 L2 水位；扩容或清理；受理侧会带 `QUEUE_FULL` 429 拒收 |
| 每令牌队列深度 | > 100（`per_token_depth_warn=100`） | 定位 queue_key，确认其 PAUSED 或积压原因；必要时人工关账该批次 |
| PAUSED_AUTH 队列数 | > 0 持续 10min（`paused_auth_warn_seconds=600`） | 查令牌拉取失败率 + 审计 `TOKEN_FETCH` 记录；确认 aitest 令牌接口 / 授权状态 |
| 令牌拉取失败率 | > 10%（`token_fetch_fail_rate=0.10`） | 核对 aitest API 可用性；按手册 01 确认授权记录；避免队列整体 PAUSED_AUTH 恶化 |
| 回调失败率 | > 5%（`callback_fail_rate=0.05`） | 查 aitest 回写可达性与重试队列；确认回调合并节流未堆积 |
| 回调待发送积压 | > 10min（`callback_backlog_seconds=600`） | 同上；必要时手动核查积压任务列表 |
| 热记忆物化失败 | > 0（`materialize_fail`） | 查刷新任务与 `memory_hot_file.generated_at`；重跑物化 worker |
| WAITING 超时转人工 | > 0（`waiting_timeouts`） | 引用依赖异常：核对被引用轮次是否未收敛 |
| AI Worker 利用率 | < 50%（有待派发工作时）或 ≥ 90% 持续 | 过低查上游路由，过高按 §8.2 调整 W |
| L2 磁盘水位 | WARN 70% / FORCE 85%（`watermark_warn/force_fraction`） | FORCE 档按淘汰优先级（未评价→无效→有效，不碰 RUNNING 批次）强制清理；仍不足则受理拒收 `DISK_FULL` 507 |
| 审计哈希链校验失败 | 任意一次失败即告警 | 见下方「审计哈希链失败」 |
| fencing 拒绝写入 | > 0 持续 10min（`fencing_reject_warn_seconds=600`） | 提示重复执行：核对旧 Worker 进程是否已确认终止（租约回收前不得重派） |

spec §7.3 全表见设计文档 935-957 行；未在本表列出的项（如探针成功率、分桶 P95）走日志聚合 / SLS 侧告警，不在内置告警判定的实现范围内。

## 2. 关键告警处置细则

### 2.1 QUEUE_FULL / DISK_FULL（受理侧 429 / 507）

1. 确认返回体 `retryable=true` 与 `retry_after`（aitest 待发队列按退避重试，不丢消息）；
2. 检查全局队列深度（限 500）与各令牌队列深度（限 100），以及 L2 水位（WARN 70% / FORCE 85%）；
3. 处置：扩容 / 清理磁盘 / 提高水位阈值（`AITRACE_STORAGE__WATERMARK_FORCE_FRACTION`）；持续 5 分钟以上必须响应。

### 2.2 PAUSED_AUTH > 10min

1. 审计 `TOKEN_FETCH`（`SELECT * FROM audit_log WHERE action='TOKEN_FETCH'`，detail 含 result/code）；
2. 验证 aitest 令牌接口从 AITrace 主机可达且 read_token 未过期；
3. 确认对应 queue_key 的授权记录在 aitest 侧存在（404/403 → 任务 `TOKEN_REQUIRED` 失败而非暂停，属正常语义）；
4. 若为批量授权失误：aitest 侧补授权后，队列退避到点自动恢复，不需要人工解挂。

### 2.3 审计哈希链失败（最高优先级）

1. 审计服务已内置熔断：哈希链校验失败期间 `AuditService.append` 抛 `AUDIT_CHAIN_BROKEN`（503），停止一切审计写入；
2. 立即停止新投递（灰度闸 `AITRACE_GRAYSCALE__ENABLED=true` + 空清单可整体拒绝投递，见手册 05）；
3. 按 `prev_hash`/`hash` 逐行核对 `audit_log`，定位断裂点（`verify_chain` 逻辑见 `src/aitrace/core/audit.py`）；
4. 排除人为篡改后修复完整性，重启服务恢复审计写入（熔断状态随之消解）；
5. 排查期间服务可降级运行但审计缺失，事后必须补齐人工记录。

### 2.4 fencing 拒绝写入持续

1. 典型根因：租约回收时旧 Worker 未确认退出即重派，旧进程后续写入被 CAS 拒绝；
2. 排查：`SELECT task_id, lease_owner, lease_generation FROM task_queue WHERE status='AI_RUNNING'` 对照在途进程；
3. 处置：确认旧进程退出（回收流程要求先终止并确认），再放恢复器重派；禁止在不确认进程退出的情况下手动重派任务。

### 2.5 探针成功率 < 90%（spec §7.3 阈值）

1. 检查目标机资产范围配置 `AITRACE_PROBE__HOST_ALLOWLIST`（默认回环才放行，未配置时非回环一律拒绝 `PROBE_FORBIDDEN`）；
2. 核对目标机 SSH 可达性与命令白名单（全局 fullmatch ∩ 画像前缀）；
3. 审计 `SSH_PROBE_DENIED` / `SSH_PROBE_TIMEOUT` 定位是闸拒绝还是连通性超时。