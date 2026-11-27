# 05 · 灰度扩量运维手册

## 1. 闸语义（实现口径）

- `AITRACE_GRAYSCALE__ENABLED=false`（默认）：闸关闭，全力放行——所有产品线正常投递；
- `AITRACE_GRAYSCALE__ENABLED=true`：**仅放行 `AITRACE_GRAYSCALE__PRODUCT_IDS`（JSON 数组）内产品线**；
- 清单为空 + enabled=true：拒绝一切投递；
- 被拒绝响应：`403 GRAYSCALE_BLOCKED`，`retryable=false`（aitest 待发队列**不重试**，需人工加入清单后重新触发）；闸位于背压检查之前（先鉴后压），不消耗队列深度；
- 生效位置：`src/aitrace/api/routes/deliveries.py::create_tasks`（`settings.grayscale`）。

## 2. 扩量流程

1. **选线**：挑 1~2 条画像已完善（领域画像 + 置信度权重 + 探针白名单齐全）且用例量适中的产品线；
2. **开闸**：
   ```bash
   export AITRACE_GRAYSCALE__ENABLED=true
   export AITRACE_GRAYSCALE__PRODUCT_IDS='["ssd_9560"]'
   ```
   重启服务后生效（配置为启动装载）；
3. **观察**（建议 ≥ 1 周或 ≥ 20 个真实批次，见 spec §8.2 阶段一标定要求）：
   - 有效率：回传结论被采纳 / 回调接收成功率（> 95% 为健康）；
   - 轮次收敛：重诊占比、平均轮次；引用级联是否正常收敛；
   - 异常率：`TOKEN_REQUIRED` / `VALIDATION_FAILED` / `INTERNAL` 占比；
4. **扩量**：把验证通过的新产品线追加进 `PRODUCT_IDS` 清单，重启生效；每批扩量都做记录（时间 / 产品线 / 依据）。
5. **全量放开**（二选一，必须做记录）：
   - 清单扩至全部产品线；或
   - `AITRACE_GRAYSCALE__ENABLED=false` 关闭闸。

## 3. 观察指标与退出条件

| 指标 | 健康线 | 异常动作 |
|---|---|---|
| 回调接收成功率 | > 95% | 查手册 02「回调失败率」处置 |
| PAUSED_AUTH 队列 | 0 持续 | 查授权记录 |
| 结论有效率 / 重诊占比 | 无异常突增 | 回看画像置信度权重与本领域 case 库 |
| `GRAYSCALE_BLOCKED` 计次 | 仅预期的未放行产品线 | 确认误拦产品线并即时补清单 |

退出条件：任意一条健康线持续不满足且回退前无法修复 → 执行回退。

## 4. 回退动作

- `AITRACE_GRAYSCALE__ENABLED=false` → 重启 → 立即恢复全开放（闸语义见第 1 节）；
- 或缩减清单回已验证产品线；
- 被 `403 GRAYSCALE_BLOCKED` 拒绝的产品线投递不会自动补发；恢复放行后由 aitest 重新触发。