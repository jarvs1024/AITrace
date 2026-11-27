# R1 合规评估报告（上线 gate）

评估对象：AITrace V1（spec `2026-11-25-aitrace-design-v1-final.md` §7.2）。
评估时间：2026-11-27（M5 收口）。
评估方式：代码走查 + 安全测试集 + 红队用例，不依赖口头声明。

> **Gate 声明：R1 三条红线合规评估通过前，不得接入真实被测日志。**

## 1. 红线 1 · 令牌安全 —— 结论：通过（带限定）

| 控制 | 实现位置 | 验证 |
|---|---|---|
| 投递载荷不携带 `qoder_access_token` | `src/aitrace/api/schemas.py`（`_StrictModel` `extra="forbid"`） | 契约测试集（integration/e2e） |
| JIT 拉取，仅内存持有，任务结束销毁 | `src/aitrace/ai/token_client.py`（TokenFetchClient，服务级 HTTPS + 超时） | `tests/unit/test_token_client*.py` |
| 每次拉取入审计，且绝不记令牌本体 | `src/aitrace/ai/worker.py::_audit_fetch`（detail 仅 product_id/batch_id/case_id/trigger_user_id/result/code/retryable） | `tests/security/test_redteam_token.py`（恶意令牌体 + `<script>` 注入 + 日志卫生红队） |
| 队列/缓存不存令牌 | `user_queue.last_error` 仅错误码/脱敏摘要（spec §5.2）；无令牌键路径 | 代码走查 + `make test-security` |
| 令牌过期/吊销恢复链路 | fencing 拒绝旧写入，任务回 READY，`PAUSED_AUTH` 指数退避 | `tests/e2e` 执行中过期用例 + unit |
| SDK 只允许内存注入 | `src/aitrace/ai/qoder.py`（`sdk.access_token(token)` 内存注入；无 accessTokenFromEnv / qodercliAuth / 本地登录态路径） | `make test-security` |

限定：真实链（GLM-5.3-Flash + 真实 CN 令牌）为环境变量注入的实测链路（2026-11-26 记录于 spec 附录），生产接入前仍需按 spec §8.2 完成阶段一 20+ 真实批次标定——这是**标定要求**，不构成红线 1 的不通过项。

## 2. 红线 2 · 只读探针 —— 结论：通过

| 控制 | 实现位置 | 验证 |
|---|---|---|
| 主机资产范围闸（防伪造投递驱动 SSH 横向移动） | `src/aitrace/probe/assets.py`（host_in_assets）+ `probe/client.py` 入口首查 | `tests/unit/test_probe_assets.py` |
| 三层命令闸：DENY 搜索 → 全局 fullmatch → 画像前缀 | `src/aitrace/probe/client.py` + `whitelist.py`（写/复位/固件刷新禁词 + shell 元字符禁用模式；全局只读白名单 10 条全匹配正则） | `tests/unit/test_probe.py` + `tests/security/test_redteam_probe.py`（元字符/未知选项/SSRF 元数据主机/拒绝路径不触达 runner 红队） |
| SSH 安全参数 | `BatchMode=yes` + ConnectTimeout；**无** `StrictHostKeyChecking=no`（host key 走 known_hosts 校验） | 代码走查 `build_ssh_runner` |
| 逐次审计 | SSH_PROBE / SSH_PROBE_DENIED / SSH_PROBE_TIMEOUT 入审计哈希链 | `tests/unit/test_probe.py` |

未接线项（如实声明）：生产 app.py 尚未把 ProbeClient 装配进 AI 编排的探针子代理调用（探针闸代码已实现并被单测覆盖，但与主链路的运行时接线属 spec 后续任务）——该接线未完成前，真实链探针功能不可对外宣称可用。

## 3. 红线 3 · 运行时约束 —— 结论：通过（带未决项清单）

| 控制 | 实现位置 | 验证 |
|---|---|---|
| 本地 CLI runtime 编排（无远程执行入口） | `src/aitrace/runtime/dispatcher.py`（进程内调度 tick）；任务 runtime 目录隔离且 finally 清理 | `tests/e2e` |
| 单实例 + 任务级隔离 | task HOME 非持久（`run_once` finally `shutil.rmtree(runtime_dir)`）；沙箱目录随任务销毁 | `tests/unit/test_worker*` |
| Memory 仅本地 | 案例库/贡献/热记忆全部本地 PG + 本地文件，无外置向量库或云存储 | M3 集成测试 |

未决项清单（不构成红线 3 阻塞，但上线前须补齐）：
1. 探针运行时接线（见红线 2 未接线项）；
2. 阶段一真实批次标定（spec §8.2，20+ 批次）；
3. AI 任务 P95 基线 D 仍是初始值 360s，待真实链标定后收敛（spec §7.1 M4 口径）。

## 4. 评测总表

| 红线 | 结论 | 依据命令 | 结果 |
|---|---|---|---|
| 1 令牌安全 | 通过（带标定限定） | `make test-security` + `tests/security/test_redteam_token.py` | 全绿（M5 实测） |
| 2 只读探针 | 通过（探针闸本体；运行时接线未完成） | `tests/unit/test_probe.py` + `test_probe_assets.py` + `tests/security/test_redteam_probe.py` | 全绿（M5 实测） |
| 3 运行时约束 | 通过（带未决项清单） | `make verify`（lint + test + contract + e2e） | 全绿（M5 实测） |

## 5. 放行结论

三条红线**控制本体**均通过评估；存在三项不构成红线违反、但必须在真实链路启用前闭环的未决项（探针接线、阶段一标定、SLO 基线收敛）。

**放行结论：R1 gate 判定为通过（条件放行）——允许进入下一里程碑的接线与标定工作；在未决项 1（探针接线）完成前，不得对真实被测日志启用探针类分析。**