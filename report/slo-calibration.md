# AITrace V1 SLO 标定基线（M4）

> 数据源：`make perf` 快配实测（harness `tools/perf_harness.py --profile all --batches 8`），复跑时间 2026-11-27T09:43Z（报告 `/tmp/perf_report_t9.json`，参数 batches=8、ai_workers=2、测试库 `aitrace_test`）。本文档回填 spec §7.1「实测（P95）」列与 §8.2 C/W/D 收敛值（见 docs/superpowers/specs/2026-11-25-aitrace-design-v1-final.md）。

## 实测结果与 SLO 对照

| 指标 | 目标 | 实测（P95） | 判定 |
|---|---|---|---|
| 投递受理确认 | < 5s | 0.028s（burndown 8 批）/ 0.042s（fairness 3 用户） | 达标 |
| 排队转清洗（QUEUED→CLEANING） | < 120s（全局未饱和） | 22.6s（burndown）/ 11.8s（fairness） | 达标 |
| 令牌队列等待（前序 0 / 1–4） | < 60s / ≤ 4×D | 0.0s / 0.0s（各分桶） | 达标（fake AI 口径） |
| 跨用户公平性（同前序深度桶） | ≤ 2×D（720s） | 0.0s / 0.0s / 0.0s | 达标 |
| 单 case 端到端（前序 0 / 1–4） | < 900s / < 2700s | 1.60s / 22.63s（burndown）；8.73s / 10.75s（fairness） | 达标 |
| 批次完成 | < 3600s | 23.86s（burndown）/ 12.19s（fairness） | 达标 |
| 回调风暴（送达） | 回调总量 ≥ 会话数且 ≥ 1 | 24/24（burndown）；12/12（fairness） | 达标 |
| 受理背压 | QUEUE_FULL 拒收 + retry_after | 投递 20 / 受理 10 / 429 × 10，retry_after P95 60s | 达标 |

**汇总：burndown 8/8、fairness 11/11，无未达标项。**

## 收敛值（§8.2）

| 参数 | 收敛值 | 依据 |
|---|---|---|
| 清洗池并发 C | **4（保持）** | 快配 8 批 × 3 例并行清洗下排队转清洗 P95 22.6s，吞吐量级达标 |
| AI Worker 池 W | **2（V1 默认）** | 快配以 W=2 实跑 burndown/fairness 全 SLO 达标、AI 槽位无等待——按「实测 W=2 更稳」裁定；扩容仅调配置，不引入多进程（§8.4） |
| AI 任务 P95 基线 D | **360s（维持初始值）** | 快配为 fake AI 即时完成口径，队列等待阶梯全 0.0s，无法标定真实 AI 时长；待真实链（GLM-5.3-Flash）阶段一标定后再收敛 |
| 15/45/60min 目标 | **快配全达标，保持** | e2e 前序 0 1.6s / 前序 1–4 22.6s、批次完成 23.9s；限定词见 spec §7.1 |

## 未覆盖项（不承诺实测值）

- 清洗 + 切片单项耗时（harness 聚合口径为投递→CLEANING）
- 探针时效（harness 未接入探针段；探针语义由 M3 三态审计保证）
- 大日志分桶、>4 前序过载桶
- 真实模型链队列等待阶梯（D 标定依赖有效令牌的真实 AI 链）

阶段一 20+ 真实批次采集仍是上线前必须完成的标定动作；快配仅作 harness 与目标对照验证。