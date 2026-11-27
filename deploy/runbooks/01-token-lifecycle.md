# 01 · 令牌生命周期运维手册

适用对象：AITrace 运维 / 安全值班。涉及真实 qoder-cn 令牌的操作仅允许在运维侧环境变量中完成，任何步骤禁止把令牌写入文件、数据库、队列、缓存或应用日志。

## 1. 令牌模型（JIT 拉取）

- 知因（AITrace）**不接收也不存储** `qoder_access_token`。投递契约即禁止该字段：`DeliveryPayload` 为 `extra="forbid"` 的严格模型（`src/aitrace/api/schemas.py`），携带未知字段直接 422 拒收。
- AI 任务派发前，ai-worker 按投递键（`product_id` / `batch_id` / `case_id`）+ `trigger_user_id`（任务投递键 `queue_key`）经 aitest 内部接口 `GET /api/v1/aitest/token` JIT 拉取：aitest 侧校验 case 归属与用户授权记录后返回 `token` + `expires_at`。
- 三红线：
  1. **仅 HTTPS**：服务级 read_token 走 HTTPS 通道拉取；
  2. **仅内存**：令牌仅在任务执行期的内存句柄中存活，任务结束立即销毁；
  3. **绝不落库/队列/缓存/应用日志**：`user_queue.last_error` 只存错误码与脱敏摘要；审计 `TOKEN_FETCH` 的 detail 只记 `product_id/batch_id/case_id/trigger_user_id/result`（失败另加 `code/retryable`），结构上不存在 token 键（`src/aitrace/ai/worker.py::_audit_fetch`）。
- 待办主体的令牌真正值只存在于环境变量（如 `AITRACE_AI_TOKEN` / 启动环境中注入的内存变量），**禁止**写入部署仓库、`.env` 提交物或任何配置文件。

## 2. 轮换步骤（aitest 侧令牌更换）

1. aitest 侧在托管库轮换用户令牌（旧令牌标记为待吊销）；
2. AITrace 侧下一次 JIT 拉取返回 `401 TOKEN_INVALID` / `410 TOKEN_EXPIRED`；
3. 对应用户队列（`queue_key` = 触发人）自动进入 `PAUSED_AUTH`，指数退避重查（1/2/4/8…上限 30 分钟），其他用户队列不受影响；
4. aitest 侧新令牌生效后，恢复器（`recover_paused`）到点恢复队列并放行 1 个探针任务重新拉取，成功即续跑——**无需重启服务**；
5. 验证：`make test-security`（覆盖 TOKEN_EXPIRED → PAUSED_AUTH 恢复链路与 TokenFetchClient 分类语义）。

## 3. 吊销与在途清除

- 吊销后在途任务：下一次拉取返回 401/410，任务回 `READY`、队列 `PAUSED_AUTH`。
- 执行中令牌过期：当前执行作废，fencing（`lease_owner + lease_generation` CAS）拒绝其后续写入，任务回 `READY` 且 `attempts+1`，队列重派；超过 `max_attempts` 才置 `FAILED(TOKEN_EXPIRED)`。
- `PAUSED_AUTH` 超过上限（默认 6 小时）：该队列等待令牌的 AI 任务批量置 `FAILED(TOKEN_REQUIRED)`，队列回 `ACTIVE`，只向 aitest 发一次 `QUEUE_STATE/ABANDONED` 聚合事件。
- 不可恢复的未托管 / 无授权（404 / 403）：只置当前任务 `FAILED(TOKEN_REQUIRED)`，不暂停队列。

## 4. 禁用项（SDK 侧）

- 只允许 `sdk.access_token(token)` 内存注入（`src/aitrace/ai/qoder.py::CnSdkAdapter`）；
- 禁止 `accessTokenFromEnv()`、`qodercliAuth` / 本地登录态及任何会持久化令牌的认证路径；
- 禁止在 systemd unit / EnvironmentFile / 配置模板中出现真实令牌明文。

## 5. 运维自查清单

- [ ] `AITRACE_AI_TOKEN` 只存在于进程环境，`env | grep -i token` 之外无任何文件命中；
- [ ] `user_queue.last_error` 抽查无令牌子串；
- [ ] 审计表中 `action='TOKEN_FETCH'` 的 detail 无 `token` 键（有哈希链可追）；
- [ ] 应用日志 grep 无 `qk_` 令牌前缀与 `<script>` 类投递内容（红队用例 `tests/security/test_redteam_token.py` 同款断言）；
- [ ] `make test-security` 全绿。