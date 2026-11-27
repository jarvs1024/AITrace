# aitest mock

独立 FastAPI 进程，模拟 aitest 的 JIT 令牌、用例清单与回调接收端点。禁止依赖 `src/aitrace`。启动：`make mock`。

## 场景切换

- 请求头 `X-Mock-Scenario: token_not_found` 返回 404 `TOKEN_NOT_FOUND`
- 请求头 `X-Mock-Scenario: token_expired` 返回 410 `TOKEN_EXPIRED`
- `POST /__mock/reset` 清空已接收回调与投递记录，供 e2e 用例隔离
- 所有 `/api/v1/aitest/*` 与 `/__mock/*` 端点要求 `X-Aitest-Read-Token`，默认 `mock-read-token`

## 真实 CN 链令牌下发

cn 版整链实测时，mock 作为真实 aitest 平台的替身：设环境变量 `AITEST_MOCK_CN_PAT=<真实 CN PAT>` 后，
`/api/v1/aitest/token` 将直接下发该 PAT（仅驻留运行进程内存，走回环 HTTP，不落库不写日志）；
未设置时保持返回 `mock-token-<user>` 假令牌（fake 链用）。

## 控制台与运营看板

`GET /` 返回演示控制台：用例清单（`/api/v1/aitest/cases`）、推送到知因（`POST /api/v1/aitest/deliver`
透传知因 `/api/v1/aitrace/tasks`）、会话状态轮询与解析结果详情（`/api/v1/aitest/aitrace/status/{sid}`
透传知因）、回调记录（`/__mock/callbacks`）。`GET /__mock/stats` 聚合运营看板数据：累计投递、分析
会话、已完成、AI 完成率、平均置信度、根因分布、回调事件统计、最近投递与会话状态一览。回调投递
不保证顺序（RESULT 优先级高于 PROGRESS），看板按回调 `seq` 取会话最新阶段。
