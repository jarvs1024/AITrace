# AITrace

> **自动化测试失败 AI 分析进化系统**
> Automated test-failure AI analysis & self-evolution platform

AITrace 面向 SSD 制造/测试场景：从自动化测试平台**旁路**接入失败事件，结合**测试脚本代码、运行日志、测试环境**做多阶段证据链归因，输出「根因 + 置信度 + 证据链 + 责任域」，并把经人工确认的结论沉淀为可复用知识资产，驱动系统自进化。

分析失败**绝不阻塞**测试产出；确定性规则优先、AI 兜底；每条结论必须带 `evidence_id` 引用，否则拦截丢弃。

## 六大技术机制

| 编号 | 机制 | 说明 |
|---|---|---|
| M1 | 双层失败指纹 | `symptom_signature × environment_vector`，把不同表现收敛到同一根因 |
| M2 | 同批次对照归因 | DUT×环境 2×2 反事实网格 + Beta-Binomial 收缩 + Fisher 检验，剥离伪相关 |
| M3 | 不可逆性边界检测 | 首个不可恢复事件即因果起点，配合 SSD 因果先验图 |
| M4 | 假设-复现-确证闭环 | 结构化 YAML 配方 → 影子真机执行 → 签名匹配 → 置信度 ≥ 0.95 |
| M5 | 三通道自进化 | 规则蒸馏 / 策略 bandit / 测试资产反向生成，双闸门灰度发布、一键回滚 |
| M6 | 误判判别式挖掘 | 把历史误判转成下一轮的判别特征 |

**责任域分层**：L0 环境 / L1 主机驱动 / L2 测试框架 / L3 协议链路 / L4 固件 / L5 器件 / L6 规格设计

## 文档

| 文件 | 内容 |
|---|---|
| [完整设计文档 V0 初稿](docs/superpowers/specs/2026-11-08-aitrace-design-v0.md) | 宏观架构（三阶段 + 四级漏斗 + 代码事实抽取）、七层实现分层、领域模型与数据资产、六大机制、提示工程、通知协同、接口契约、十二项技术难点、专利布局、路线图、度量体系 |
| [架构图 V0 初稿](architecture-v0.svg) | 宏观架构：平台供料 → 阶段 1 漏斗筛选 → 阶段 2 AI 深诊 → 阶段 3 通知闭环 → 知识资产库·自进化中枢（SVG · [PNG](architecture-v0.png) · [HTML](architecture-v0.html)） |

> 架构图与设计文档 §2 逐项对应：**图为骨架，字为细节**。后续版本以 `architecture-v0` 为基线分叉。

## 技术栈

Python 3.11 · FastAPI · Pydantic v2 · PostgreSQL 16 + pgvector · Redis · MinIO · numpy/scipy · Qoder Agent SDK + 自研 `LLMGateway` 抽象 · Prometheus / Grafana

## 现状

M0 冷启动中（历史失败数据几乎为零，从零积累）。平台侧关键路径：新增 `bench_topology` 表、`run_group_id`、失败事件推送 API、制品访问方式。
