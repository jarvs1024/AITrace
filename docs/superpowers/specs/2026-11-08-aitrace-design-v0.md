# AITrace（智溯）· 自动化测试失败 AI 分析进化系统 · 总体设计文档

> **V0 初稿** ｜ 日期 2026-11-08 ｜ 状态：设计评审稿
> 配套架构图：`architecture-v0.svg`（**V0 初稿 · 宏观架构**），本文档 §2 与该图逐项对应
> 适用对象：SSD 固件/硬件测试团队、测试平台研发、平台架构组

---

## 0. 摘要与决策摘要（TL;DR）

### 0.1 一句话定义

**AITrace（智溯）** 是一个**在 SSD 自动化测试平台旁路运行的、把"失败用例"变成"可执行根因结论 + 可复用知识资产 + 可自我改进的分析策略"**的闭环系统。它不止回答"哪里错了"，而是回答"**谁错了、凭什么这么判断、怎么复现、下次怎么更快地判**"。

**命名释义**：**AI** = 智能归因引擎；**Trace** = 从失败表象逐层追踪到因果起点，并留下完整可回溯的证据链；**智溯** = "智能 + 溯源"双关，指向"由 AI 溯出根因"。系统英文代号 `AITrace`，对外中文名「智溯」，服务名 `aitrace`。

### 0.2 与"用 AI 读日志"的本质区别

市面上的做法基本是：日志 → LLM → 一段文字。本系统与之的差异在于六个**结构性机制**（这六点也是专利布局的落点）：

| # | 机制 | 技术问题 | 技术效果 |
|---|---|---|---|
| M1 | **双层失败指纹**（symptom signature × environment vector） | 相同日志跨机器/跨盘号/跨时间戳无法聚合，知识库一合并就污染 | 跨环境可迁移复用 + 环境敏感的根因不被错误合并 |
| M2 | **同批次对照归因**（counterfactual control-group） | SSD 测试 40%~60% 的"失败"其实是测试系统/环境造成的被测件误报 | 用对照组把被测件缺陷与环境故障在统计上分离，误报率可下降一个量级 |
| M3 | **不可逆性边界检测**（causal origin reconstruction） | 日志最后一条错误通常是症状不是根因 | 自动定位因果链起点（首个不可恢复事件），而非末条报错 |
| M4 | **假设-复现-确证闭环**（hypothesis→execution→confirmation）· *V0 延后 M3* | AI 结论不可验证，无法采信 | 把"断言"升级为"机器复现确证"，根因可信度从 60% 提到 95%+ |
| M5 | **三通道自进化**（规则蒸馏 / 策略择优 / 测试资产反向生成） | 知识只增不改，系统越用越慢越乱 | 同类问题第二次出现成本降 90%，且自动产出回归用例防逃逸 |
| M6 | **误判判别式挖掘**（discriminative feature mining） | 人工"拒绝"这一条低价值信号被浪费 | 从每次误判中自动提炼"防止此类误判的判别规则"，知识库单调增强 |

### 0.3 关键设计决策（本次评审需要拍板的）

| 决策点 | 选定方案 | 理由 |
|---|---|---|
| AI 接入方式 | **Qoder Agent SDK（Python）为主**，Cloud Agent 承担长任务/批量分析，本地 CLI 承担涉密日志 | 符合既有工具链；Qoder 提供 Agent Harness（任务规划/工具调用/上下文管理/会话记忆），省掉自建 agent 框架 |
| 历史数据 | **无历史，从零冷启动** | 设计必须以"无标注"为第一性约束：主动学习 + 无监督聚类 + 领域先验知识库 |
| 平台改造 | **可加模块**（新开发 API + 页面嵌入） | 分析系统做成独立服务 + 可嵌入组件，不侵入主流程 |
| 通知 | **钉钉机器人 + 钉钉工作通知** | 支持卡片交互（采纳/纠正/驳回）直接在钉钉完成，形成最短反馈闭环 |
| 规模 | 日均几百~上千用例，几十台服务器 | 单集群可承载，PostgreSQL + 对象存储 + 轻量队列足够，无需引入重型分布式组件 |
| 专利主线 | **自进化闭环**，但落到 M5/M6 的具体技术手段 | 泛化的"AI 自学习"易被驳回，必须落到可量化的技术手段与效果 |

### 0.4 预期收益（保守估计）

| 指标 | 现状（假设基线） | M4 目标 |
|---|---|---|
| 单次失败初判耗时 | 30~90 分钟人工 | ≤ 3 分钟自动 + 5 分钟人工确认 |
| 失败归类正确率 | ~70%（人工经验） | ≥ 92%（含复现确证） |
| 环境误报（被测件误判为缺陷） | 40%~60% | ≤ 15% |
| 重复问题平均定位时长 | 接近首次 | 首次的 1/10（资产命中） |
| 逃逸缺陷中"曾出现过"的占比 | 未统计 | 提升至 ≥ 60%（逃逸分析能力） |

---

## 1. 背景与问题定义

### 1.1 现状

- 团队以 **Python 自动化测试**为主，测试对象为 **Linux 服务器直连的 SSD 盘**。
- 已有一个内部测试平台，承载测试结果展示，但**分析能力不完善**：失败即呈现原始日志，不做归因、不做聚合、不沉淀。
- AI 工具链使用 **Qoder**（IDE/CLI/Cloud Agents/Agent SDK）。
- 日常痛点：失败即"群里 @ 人"，人工翻日志，重复问题重复分析，新人无法独立定位。

### 1.2 问题的真实形态（SSD 领域特殊性）

一个 SSD 自动化测试的"失败"不是单一形态，至少需要区分五类，它们要求完全不同的处置动作：

| 类别 | 典型现象 | 正确处置 |
|---|---|---|
| **A. 被测件真缺陷（被测件）** | UECC、掉盘、固件断言、PLP 掉电丢数据、寿命提前耗尽 | 提缺陷单、进研发 |
| **B. 固件版本回归** | 同型号上一版本正常，新版本大面积失败 | 版本拦截、禁入发布 |
| **C. 规格/设计裕量不足** | 长稳/高温下极限触发，但符合规格 | 记为 Limit 项，非缺陷 |
| **D. 测试系统误报（基础设施）** | 背板/线缆/PCIe 链路 CRC、供电跌落、散热、RAID 卡、主机 OS/driver、测试框架自身 bug | 修测试系统，不提缺陷单 |
| **E. 随机/不可复现（Flaky）** | 同条件重跑通过，无稳定模式 | 隔离复跑、加判据，不轻易定性 |

**这是整个系统的核心价值命题**：如果把 D（环境误报）当成 A（被测件缺陷）提交，会污染缺陷库、误导研发、并让真实缺陷被淹没；反过来把 A 当成 D 吞掉，则会漏掉批次性质量事故。所以**归因的准确性比归因的速度重要一个量级**。

### 1.3 系统目标与非目标

**目标（G）**
- G1 自动化输出**带证据链的根因结论**（责任域 + 置信度 + 支撑/反对证据），而非自然语言猜测。
- G2 结论**可被机器复现确证**，形成"确证资产"。
- G3 失败结论**沉淀为结构化知识资产**（失效模式卡/检测规则/复现配方/回归用例），可检索可复用。
- G4 系统**随使用自我进化**：同类问题第二次出现，成本与耗时显著下降，且准确率不下降。
- G5 失败结果**主动通知到人**，分级、去重、降噪，且**通知中直接可完成反馈采纳**。
- G6 输出**专利可保护的机制**，具备可量化的技术效果。

**非目标（NG）**
- NG1 不替代测试执行平台，只做"失败后的分析层"。
- NG2 不做固件/驱动的自动修复（超出测试团队职责与授权边界）。
- NG3 不做通用日志分析平台，**强绑定 SSD/NVMe/PCIe 领域语义**（这是护城河，也是专利必要特征）。
- NG4 第一阶段不做自动提单到研发系统（避免污染缺陷库），先在平台内闭环。

---

## 2. 总体架构

> 本节与架构图 **`architecture-v0.svg`（V0 初稿 · 宏观架构）** 逐项对应：**图为骨架，字为细节**。

### 2.1 宏观全景：一条主链 + 一条旁路 + 一个资产底座 + 一层只读度量视图

```
  Aitest 自动化测试平台（外部 · 双向对接 · 不改平台代码与平台数据）
        │  供料 ▼：失败用例日志 · 编排分组 · 环境信息 · 测试脚本
        │  回传 ▲：结论 · 证据链 · 处置建议（平台前端「AI 分析」Tab 展示）
        ▼
 ┌───────────────┐    ┌───────────────┐    ┌───────────────┐
 │  阶段 1        │───▶│  阶段 2        │───▶│  阶段 3        │
 │  多级数据清洗   │    │  AI 深度诊断   │    │  结果分类与     │
 │  与漏斗筛选     │    │  引擎（Qoder） │    │  群通知闭环     │
 └───────────────┘    └───────────────┘    └───────────────┘
        │ 免 AI 截流（环境类异常直达通知）        │ 人工反馈
        └──────────────────────────────▶  ┌──────────────────┐
                                           │  知识资产库      │
                                           │  · 自进化中枢    │
                                           └──────────────────┘
                                                    │ 只读取数
                                           ┌──────────────────────────┐
                                           │  经验运营看板（只读视图）│
                                           └──────────────────────────┘
```

**五个边界前提**

1. **双向对接，但不改平台**：平台代码、平台数据库与用例库均不被本系统修改。数据通道是双向的——**供料方向**由平台提供失败用例日志、编排分组、环境信息与测试脚本；**回传方向**由本系统把结论、证据链与处置建议经标准接口返回，在平台前端的「AI 分析」Tab 内展示（§8.1）。平台数据面保持只读：回传的是**读取与展示**，不是对平台数据的写入。
2. **平台供料边界（V0 收紧）**：Aitest 平台只需提供四项——**失败用例日志**、**执行编排与批次分组**、**测试环境信息**、**测试脚本仓库**。**不再要求平台暴露拓扑快照与用例失败断言详情**：批次对照视图由 AITrace 用「批次分组 + 环境信息」自建，断言语义由脚本抽象语法树解析得出（§2.2.1）。代价是失去真正的反事实 2×2 对照网，M2 降级为同批次共因子对照（§2.3.3）。
3. **旁路部署**：分析失败绝不阻塞测试产出，任何解析/AI 异常一律降级为"分析不可用"。
4. **闭环在人工**：结论必须经人工采纳才允许进入资产库——自进化的燃料是**人的确认**，不是模型的自说自话。
5. **看板只读**：经验运营看板只做观测与统计，不参与诊断判定、不回写任何资产，也不构成闭环出口；看板不可用不影响主链路（§2.6）。

### 2.2 阶段 1 · 多级数据清洗与漏斗筛选

第一道关卡是**降本**：日均几百~上千用例全量进 AI 既慢又贵，且噪声会淹没真因。因此采用五级漏斗，只有疑难切片才消耗算力。

| 级别 | 名称 | 判据 | 产出 |
|---|---|---|---|
| 一级 | **初筛** | 本地确定性规则 + 大面积环境异常拦截 | 放行 / 截流 / 丢弃 |
| 二级 | **细筛 · 证据体检** | 证据完整度 · 可判定性 · 上下文预算 | 证据包 + 补采任务 |
| 三级 | **精筛 · 策略路由** | 求解链选择与成本闸门 | 求解链 + 上下文预算 |
| 四级 | **指纹去重** | 同类失败合并 | 指纹聚合簇（一条通知） |
| 五级 | **级联抑制** | 只保留链路上的首个失败点 | 单点候选 |

| 级别 | 作用 | 技术效果 |
|---|---|---|
| 一级初筛 | 本地规则 + 大面积环境异常拦截 | 规则命中即结论，**零 AI 成本** |
| 二级细筛（证据体检） | 回答"**证据够不够、能不能判**"：日志 / 环境 / 脚本解析是否齐备、失败意图能否归一、缺什么先补采 | 避免带残缺证据进 AI，长日志压到可推理规模 |
| 三级精筛（策略路由） | 回答"**用什么方式判最划算**"：命中失效模式卡走检索快链、放大类走链式推理、掉盘类走因果图谱 | 同一漏斗内选最省的求解链；也是 M5 策略择优（多臂老虎机）的唯一落点 |
| 指纹去重 | 同类失败合并为一条通知 | 通知量降一个量级 |
| 级联抑制 | 只保留链路首个失败点 | 避免"一个根因报十次" |

> **为什么必须拆成二级 + 三级**：原 V0 把二级与三级写成同一件事（疑难故障切片、压缩上下文），两级判据完全重合，合并后只剩一个精筛层级，且 M5 策略择优在全图找不到决策位点。拆分后职责按「**能不能判**」（二级细筛）与「**怎么判**」（三级精筛）分开，两级各自可独立度量（缺证率 vs 求解链命中率），策略择优也获得落点。

#### 2.2.1 代码事实抽取（静态解析 + AI 二次核实）

**这是与"日志分析"类系统的分水岭**：只看日志，无法知道"这条断言本来想验证什么"。

代码事实的产出走**两步**，缺一不可：

```
第一步 · 静态解析（确定性，机器算）
  Python 抽象语法树 → 用例清单 · 参数矩阵 · 断言位置 · 依赖有向无环图
  特点：一次性成本（脚本变了才重解析）、可复现、枚举不漏

第二步 · AI 二次核实（语义，模型判）
  输入 = 静态解析结果 + 自动化仓库上下文（脚本片段 / 注释 / 用例名）
  → 核实断言语义：这条断言本来想验什么
  → 核实失败意图判定：不一致 / 超时 / 掉盘 / 越界
  → 复核依赖边是否成立
  → 核实通过才入库；不通过则标记"待确认"，进人工复核队列
```

| 产物 | 由谁产出 | 说明 |
|---|---|---|
| 用例清单 · 参数矩阵 | 静态解析 | 每个用例的参数组合与覆盖意图 |
| 断言位置 · 日志 → 代码行锚点 | 静态解析 | 把日志中的失败点映射回具体代码行与断言 |
| 依赖关系有向无环图 | 静态解析 + AI 复核 | 用例间的数据/资源依赖，用于级联抑制与复现配方裁剪 |
| 断言语义 | AI 核实 | 断言类型（不一致 / 超时 / 掉盘 / 越界）与判定意图 |
| 失败意图判定 | AI 核实 | 区分"数据不一致""超时""设备掉盘"等失败意图 |

**为什么是两步而不是一步**：

- 静态解析负责**枚举与定位**（有多少、在哪、依赖谁）——确定性、可复现，AI 不可靠；
- AI 负责**语义归纳**（这条断言想验什么）——静态解析做不到；
- **不让 AI 直接读仓库替代静态解析**，否则级联抑制、指纹的失败意图维度、复现裁剪都失去稳定底座（依赖边与断言位置每次可能不同 → 同类失败合并漂移、通知量失控），且每条失败事件都要付一次 LLM 成本，与漏斗降本目标冲突；
- AI 的**核实范围按需触发**：只核实本次分析引用到的用例与断言，不做全仓重读；核实结果按 `script_path + script_version` 缓存，脚本未变则复用。

产物沉淀为**代码事实图谱**（§2.5），跨版本复用。

#### 2.2.2 免 AI 截流旁路

一级初筛判定为**环境类异常**（大面积同签名失败、基础设施告警伴随、同批次共性环境异常）时，**不经 AI 直接生成通知**：

- **优点**：不消耗算力、延迟秒级、避免模型在噪声上编故事
- **约束**：只允许输出**环境/基础设施类**结论，禁止输出被测件缺陷结论（避免误伤）
- **反哺**：旁路结论同样入资产库反哺规则库，命中率随时间上升

### 2.3 阶段 2 · AI 深度诊断引擎（Qoder）

#### 2.3.1 代码-日志对齐

把阶段 1 的断言语义与日志锚点绑定：**调用栈定位失败行 → 区分读错 / 写错 / 超时 → 推断失败意图**。这是"结合自动化代码分析"的核心落点，也是本系统区别于纯日志分析器的护城河。

#### 2.3.2 SSH 实时探查（严格护栏）

失败后对目标主机做**只读**实时探查（盘状态 / 内存 / 硬件）：

- **仅失败后**触发、**只读**、**超时熔断**、**不干扰在测**
- 探查结果作为**证据**进入证据链，不直接作为结论

#### 2.3.3 同批次对照归因

用被测件×环境对照组与统计显著性判定，分离 **被测件缺陷 vs 环境误报**（M2 详见 §4.2）。

**V0 降级说明**：平台不再暴露拓扑快照，无法构造真正的 2×2 反事实网。V0 改用**同批次共因子对照**——以批次分组（`run_group_id`）为对照单元、以环境信息中的共有因子（主机 / 板卡 / 固件版本）做分层，比较「同批次同环境下其它被测件是否同样失败」。判定力弱于反事实网格，但足以剥离共性环境误报；拓扑快照若后续可得，再升级回 §4.2.2 的完整网格。

#### 2.3.4 基线与变更对比

与最近基线对比判定**新增 / 回归 / 已知**，并关联最近的固件/脚本变更。**M0 起逐步启用**（冷启动期基线不足，先只做"已知"匹配）。

#### 2.3.5 复现配方生成 + 影子执行（V0 移出主路径）

生成结构化复现配方并投放到影子真机执行，确认后回填置信度（≥ 0.95）。占用真机需成本控制，**V0 先不纳入**：架构图上不画该环节，资产库也不设「复现配方库」。

**连带影响（V0 必须显式承认，不要装作没有）**：

- **置信度天花板下移**：无复现确证，AI 单通道给出的置信度上限约 0.9，`≥ 0.95` 在 V0 不可达；
- **M4（假设-复现-确证闭环）整体延后到 M3**（见 §12 路线图），§4.4 保留为后续版本机制；
- **P0 的"已复现确证"条件失效**，V0 改为「新失效模式 + 高置信 + 多源证据一致」（见 §6.1）；
- **人工反馈成为唯一确证通道**，反馈采集量因此成为 V0 最稀缺的资源（M0 出口标准里的"≥100 条反馈"是硬指标，不是软目标）；
- 复现相关的资产、接口、前端页面在 V0 全部不实现（见 §7.1、§8.2）。

**阶段 2 输出**：根因 + 证据链 + 置信度

### 2.4 阶段 3 · 结果分类与群通知闭环

| 环节 | 内容 |
|---|---|
| 失败原因分类 | 责任域分级 L0 环境 / L1 主机驱动 / L2 测试框架 / L3 协议链路 / L4 固件 / L5 器件 / L6 规格设计（详见附录 B） |
| 置信度分级 | P0 / P1 / P2 / P3（**内容维度**，与分发维度相互独立） |
| 群通知 | 钉钉卡片推送，结论内可直接操作 |
| 人工反馈采集 | 采纳 / 纠正 / 驳回 —— **闭环进化的唯一燃料** |
| 失效模式卡归档 | **仅确认结论**才可入资产库 |

### 2.5 知识资产库 · 自进化中枢

| 资产 | 内容 | 来源 | 作用 |
|---|---|---|---|
| 一级初筛规则库 | 确认结论蒸馏而成的判定规则 | 人工采纳的结论 | 零 AI 快路径，命中率持续上升 |
| 失效模式卡 | 症状指纹 · 确认根因 · 处置手册 · 版本血缘 | 确认结论 | 同类问题直接命中 |
| 因果先验图 | 级联边 · 非缺陷超时边 | 领域先验 + 反馈增量 | 不可逆性边界判定 |
| 代码事实图谱 | 用例 · 断言语义 · 依赖有向无环图 | 静态解析 + AI 核实 | 跨版本复用 |

**演进治理**：双闸门评估（离线金标准集 + 在线影子）→ 灰度发布 → 一键回滚。

**闭环出口（V0）**：自进化必须有出口，否则只是"多存点数据，下次检索到"。V0 只保留一条出口，已画进架构图：

1. **规则回灌**——失效模式卡的确认结论蒸馏为一级初筛规则，反哺阶段 1，形成「AI 判 → 人认 → 规则判」的零成本快路径。

> **V0 的能力边界（必须写在图和文档里，不能含糊）**：
> - **测试资产反向生成（通道三）在 V0 整体不落地**，架构图上不画这条回灌箭头。M5 三通道在 V0 只有通道一（规则蒸馏）与通道二（策略择优）两条腿，通道三随 M4 补齐。
> - 其中「可执行的最小复现回归用例」需要复现配方做最小化，而复现配方库与影子执行在 V0 已移出（§2.3.5）；「预检脚本 / 判据增强 / 用例隔离建议」虽不依赖复现配方，但仍需要 confirmed 失效模式卡的积累与"人工 review 后合入测试仓库"的组织流程，V0/M0 不具备。
> - 若在 V0 画上这条箭头，就是给评审一个当前做不到的承诺；也会让人误以为失效模式卡与用例库存在数据关联（实际只有生成关系，卡内也无任何指向用例的字段）。

> **为什么资产库里没有「反馈训练信号」盒**：人工反馈（采纳 / 纠正 / 驳回）的**内容**会沉淀成失效模式卡与一级初筛规则——那已经是资产，不需要再画一个盒子重复表达；而奖励信号序列是**运行时训练信号**，本身不沉淀为资产，且它唯一的独立消费者是策略择优——**V0 三级精筛用固定策略表，多臂老虎机 M3 才启用**，所以 V0 没有它的落点。奖励信号的来源与语义在阶段 3「人工反馈采集」处交代，数据落 `feedback` 表（§3.2.6）。

> **V0 已知缺口（架构上应有位置、当前图上无落点）**：**复现配方库**（M3，§2.3.5）、**金标准集**（闸门 1 的前提，M0 起逐步沉淀）、**误判判别式护栏**（M6，M4 阶段落地）、**策略择优的奖励信号流**（M3，随三级精筛从固定策略表转为在线学习）、**测试资产反向生成的回灌出口**（M4，见 §4.5.2 通道三）。这些是后续版本的补齐项，V0 阶段不实现，但不应在评审中被当作"设计已覆盖"。

### 2.6 经验运营看板 · 资产沉淀与命中度量

**定位**：一块**只读视图**，架在知识资产库与人工反馈之上，回答四个问题——**沉淀了多少经验、命中多少、命中的是哪些、尚未覆盖与误判的情况如何**。它不参与任何判定、不回写任何资产，因此**不构成闭环出口**（V0 的闭环出口只有 §2.5 的规则反哺一条）。

| 视图块 | 回答的问题 | 主要指标 | 取数来源 |
|---|---|---|---|
| **经验沉淀量** | 可复用经验积累到什么程度 | 失效模式卡 / 一级初筛规则 / 代码事实图谱的存量与增量、版本血缘 | 知识资产库 |
| **命中情况** | 经验覆盖了多少失败 | 规则命中率、命中条数、免 AI 快路径占比 | 漏斗命中事件 |
| **命中分布** | 哪些问题已被经验覆盖 | 按失效模式卡分组的命中次数排序（Top N） | 失效模式卡检索日志 |
| **漏网与误判** | 哪些问题仍未覆盖、判错过多少 | 新增未命中族、误报率（被驳回）、待确认积压 | 人工反馈（采纳 / 驳回） |

**与前端页面的对应关系**：本块即 §8.2「进化看板 + 质量看板」的**合并首页**——首页即上表四块视图；下钻后再展开准确率、误报率、采纳率、平均定位时长、噪声分布，以及规则集版本、灰度与回滚操作。

**为什么 V0 就把它画进架构**：

1. **不新增任何分析能力**。四块的取数对象（资产、漏斗命中事件、人工反馈）在 M0 就已全部产生，看板只需一层聚合读模型，不引入新求解链路，也不消耗 AI 算力。
2. **它是"闭环是否真的在收敛"的唯一外部可见证据**。沉淀量上升只说明"积累得多"，只有命中率上升、漏网与误判下降，才能证明经验被有效复用；否则资产库只是越攒越多、越用越慢。
3. **强制暴露负向指标**。看板同时呈现未命中族、误报与待确认积压，避免只展示漂亮数字而掩盖误判护栏缺失（§2.5 已知缺口中的 M6）。
4. **与旁路原则不冲突**。看板不可用只影响观测，不影响诊断与通知链路。

### 2.7 内部七层实现分层（L0–L6）

> 上面的三阶段是**流程视角**；这里是**工程实现视角**。两者是同一套系统的两个切面。

```
┌──────────────────────────────────────────────────────────────────────────────┐
│  L6  展示与反馈层  (嵌入现有测试平台的独立前端模块)                            │
│  失败详情页 · 证据链时间线 · 失效模式卡 · 复现实验室 · 经验运营看板 · 反馈面板  │
└──────────────────────────────────────────────────────────────────────────────┘
                                    ▲ REST / SSE
┌──────────────────────────────────────────────────────────────────────────────┐
│  L5  通知与协同层  钉钉机器人 · 钉钉工作通知 · 值班路由 · 降噪与聚合 · 卡片回调  │
└──────────────────────────────────────────────────────────────────────────────┘
                                    ▲ 事件
┌──────────────────────────────────────────────────────────────────────────────┐
│  L4  进化层  规则蒸馏通道 · 策略优化通道(多臂老虎机) · 测试资产反向生成通道          │
│  评估闸门(离线金标准集 + 在线影子) · 灰度发布 · 一键回滚 · 版本血缘            │
└──────────────────────────────────────────────────────────────────────────────┘
                                    ▲ 反馈/评估信号
┌──────────────────────────────────────────────────────────────────────────────┐
│  L3  归因推理层  多求解器(规则/检索/推理/复现) · 证据图 · 不可逆边界检测       │
│                  同批次对照归因 · 因果起点重建 · 置信度校准 · 仲裁融合          │
└──────────────────────────────────────────────────────────────────────────────┘
                    ▲ 证据包              ▲ 批次对照数据
┌──────────────────────────────────────────────────────────────────────────────┐
│  L2  证据层  多源解析器(SMART/ErrorLog/dmesg/fio/traceback/串口/BMC)         │
│             归一化 · 去重 · 溯源 · 分层压缩 · 向量化                           │
└──────────────────────────────────────────────────────────────────────────────┘
                    ▲ 采集
┌──────────────────────────────────────────────────────────────────────────────┐
│  L1  接入层  平台 Webhook/轮询 · 制品收集(rsync+对象存储) · 环境/批次信息采集     │
│             执行批次编排 · 复现影子调度器(真机占用预算控制)                   │
└──────────────────────────────────────────────────────────────────────────────┘
                                    ▲
┌──────────────────────────────────────────────────────────────────────────────┐
│  L0  Aitest 平台  测试执行 · 结果展示 · 失败日志/编排分组/环境信息(需暴露)    │
└──────────────────────────────────────────────────────────────────────────────┘
```

### 2.8 关键设计原则

1. **旁路优先**：分析系统挂在测试平台旁路，**分析失败绝不影响测试产出**。任何解析/AI 异常都降级为"分析不可用"，绝不阻塞主流程。
2. **确定性优先，AI 兜底**：能用规则/字典确定的，绝不交给 LLM（成本、稳定性、可复现性三重考虑）。LLM 只处理"语义归纳"环节。
3. **每条结论必须带证据引用**：无引用的结论在输出前被拦截丢弃（工程约束，非模型自觉）。
4. **不确信的必须降级**：置信度低于阈值 → 不通知，转入"待人工确认队列"，避免误报疲劳。
5. **演进必须可回滚**：每一次进化（规则/提示/资产）都有版本号、评估分数、回滚点。
6. **人工是最终裁判，但人不做重复劳动**：人只做"确认/纠正"与"例外裁决"，不做翻日志。

### 2.9 组件职责表

| 组件 | 职责 | 技术选型 | 状态 |
|---|---|---|---|
| `collector` | 从平台拉取失败事件、收集失败用例日志/固件版本/环境信息 | Python + 平台 API 客户端 | 新建 |
| `parsers` | 多源日志解析为统一 Evidence 结构 | Python（正则+状态机+字典） | 新建 |
| **`code-facts`** | **代码事实两段式产出：① 静态解析（Python 抽象语法树 → 用例清单/参数矩阵/断言位置/依赖有向无环图/日志→代码行锚点）② AI 二次核实（断言语义/失败意图/依赖边复核），核实通过才入库** | Python（ast + 静态分析）+ LLM 核实 | 新建 |
| **`funnel`** | **五级漏斗：初筛 → 二级细筛（证据体检）→ 三级精筛（策略路由）→ 指纹去重 → 级联抑制；含免 AI 截流旁路** | Python（规则引擎） | 新建 |
| `batch-view-svc` | 基于批次分组 + 环境信息自建共因子对照视图（替代平台拓扑快照） | PostgreSQL | 新建 |
| `fingerprint` | 计算双层失败指纹 | Python | 新建 |
| `attribution` | 同批次对照归因 + 责任域判定 | Python + scipy/statsmodels | 新建 |
| **`baseline-compare`** | **基线与变更对比（新增/回归/已知），关联最近固件/脚本变更** | Python + Git 元数据 | 新建 |
| **`ssh-probe`** | **失败后只读 SSH 实时探查（盘状态/内存/硬件），带超时熔断** | Python（asyncssh/paramiko） | 新建 |
| `solver-orchestrator` | 多求解器调度、证据链推理、仲裁融合 | Python + Qoder Agent SDK | 新建 |
| `knowledge-repo` | 失效模式卡/规则/配方/回归用例/**代码事实图谱**资产库 | PostgreSQL + pgvector + 对象存储 | 新建 |
| **`insight-board`** | **经验运营看板读模型：沉淀量 / 命中情况 / 命中分布 / 漏网与误判四块视图的聚合与下钻（只读，不参与判定）** | Python（聚合查询）+ 前端读模型 | 新建 |
| `solver-router` | 三级策略路由：按失败族选择求解链（规则/检索/链式推理/因果图谱），接入 M5 策略择优 | Python + Qoder SDK | 新建 |
| `repro-scheduler` | 复现配方调度到影子真机、结果回收 | Python + 平台 API | 延后 M3 |
| `evolution-engine` | 三通道进化、评估闸门、灰度与回滚 | Python | 新建 |
| `notifier` | 钉钉通知、分级路由、卡片回调 | Python | 新建 |
| `web-module` | 嵌入平台的前端模块 | 与平台技术栈对齐（见 §8） | 新建 |

---

## 3. 领域模型与数据资产体系

### 3.1 核心实体关系

```
ExecutionRun (一次测试执行)
   │  ├─ TestCase × N
   │  │     └─ TestAttempt (含 verdict / fail_stage / artifacts[] )
   │  ├─ TopologySnapshot (主机/HBA/背板/线缆/电源/温区/DUT位置)
   │  └─ FirmwareImage (fw version + commit + build manifest)

TestAttempt(FAIL) ──1:1──> FailureCase
FailureCase ──1:N──> EvidenceItem  (溯源到原始文件/行/时间戳)
FailureCase ──1:N──> Hypothesis     (候选根因，含支撑/反对证据)
Hypothesis  ──1:1──> Diagnosis      (仲裁后的结论)
Diagnosis  ──1:N──> ReproductionRecipe (复现配方，可执行)
Diagnosis  ──0:1──> Feedback        (人工采纳/纠正/拒绝)

FailureCase ──N:1──> FailureFingerprint
FailureFingerprint ──1:N──> FailurePattern (失效模式)
FailurePattern ──1:1──> FailureModeCard (资产)

CodeFactGraph（脚本解析产出，跨版本复用）
   ├─ CaseSpec（用例清单 · 参数矩阵）
   ├─ AssertionSemantics（断言语义 · 失败意图判定）
   └─ DependencyDAG（用例依赖边）
TestAttempt(FAIL) ──N:M──> CodeAnchor（日志行 → 代码行锚点）
FailureModeCard ──1:N──> DetectionRule / ReproductionRecipe / RegressionCase

Feedback ──> EvolutionRecord (三通道进化记录，带版本血缘与回滚点)
```

### 3.2 关键数据结构

#### 3.2.1 FailureCase（失败事件）

```json
{
  "failure_id": "FC-20261108-000231",
  "run_id": "RUN-88213",
  "case": {
    "case_id": "burnin_rwd_qd64_24h",
    "suite": "burnin",
    "script_path": "tests/burnin/rwd_param_matrix.py",
    "script_version": "a3f91c2",
    "params": {"qd": 64, "block_size": "4k", "hours": 24, "pattern": "rand"},
    "fail_stage": "verify_phase",
    "assertion": "verify_bytes_mismatch"
  },
  "dut": {
    "serial": "S5Y1NX0T123456",
    "model": "SSD-9900-3.2T",
    "channel": "retail",
    "firmware_version": "F7.10.2",
    "firmware_commit": "9a1c33f",
    "nand_die_rev": "D3",
    "total_bytes_written_tb": 42.7
  },
  "env": {
    "host": "ssh-test-07",
    "hba": "HBA-PCIe-40G-02",
    "backplane": "BP-SAS-24BAY-11",
    "cable_path": "BP11-P2-C07",
    "psu": "PSU-A/B",
    "thermal_zone": "RACK-R3-Z2",
    "ambient_c": 26.4,
    "kernel": "5.15.0-105-generic",
    "nvme_driver": "nvme-2.1",
    "hba_fw": "16.200.02.11",
    "bench_topology": "shared-port-P2"
  },
  "window": {"start": "2026-11-07T22:14:03Z", "end": "2026-11-08T02:41:55Z"},
  "error_signature": "nvme_read_error_uecc",
  "raw_artifacts": [
    {"type": "case_log", "uri": "s3://ssd-test-artifacts/RUN-88213/.../case.log", "sha256": "..."},
    {"type": "smart_log", "uri": "...", "sha256": "..."},
    {"type": "dmesg",   "uri": "...", "sha256": "..."}
  ],
  "verdict": "FAIL",
  "platform_run_seq": 88213
}
```

#### 3.2.2 EvidenceItem（证据单元）

```json
{
  "evidence_id": "EV-0007",
  "case_id": "FC-20261108-000231",
  "source": {"artifact": "case.log", "line_start": 12841, "line_end": 12849},
  "t_offset_ms": 1042331,
  "event_type": "nvme_error",
  "layer": "L4_firmware",
  "normalized": {
    "opcode": "Read(0x02)", "status_code": "0x81(厂商定义)",
    "lba": "0x0000_1234_5678", "nsid": 1,
    "media_errors": 7, "retry_count": 3
  },
  "severity": 3,
  "recoverable_hint": false,
  "provenance": {"parser": "nvme-log-v2.3.1", "parser_rev": "sha256:...", "raw_sha256": "..."},
  "embedding_ref": "vec://EV-0007"
}
```

**设计要点**：`provenance` 让每条证据可回溯到原始字节，这是"可审计"与"专利实施例可复现"的要求；`layer` 字段把证据预先标注到责任域层级，使后续归因有结构先验。

#### 3.2.3 Hypothesis（候选根因）

```json
{
  "hypothesis_id": "HYP-0031",
  "case_id": "FC-20261108-000231",
  "root_cause_label": "FW_RETRY_OVERFLOW",
  "responsibility_layer": "L4_firmware",
  "suspect_entity": "FTL.retry_budget / fw 7.10.2",
  "statement": "固件在 UECC 后的重试预算耗尽，导致 IO 层超时而非立即上报介质错误",
  "support_evidence": [{"evidence_id": "EV-0007", "weight": 0.31, "relation": "supports"}],
  "oppose_evidence":  [{"evidence_id": "EV-0012", "weight": 0.08, "relation": "opposes"}],
  "prior": 0.22,
  "posterior": 0.41,
  "calibrated_confidence": 0.63,
  "produced_by": "solver_llm_chain_v3",
  "reproduction": {"recipe_id": "RC-0091", "status": "scheduled"}
}
```

#### 3.2.4 FailureFingerprint（双层失败指纹）

```json
{
  "fingerprint_id": "FP-7C2A",
  "symptom_signature": {
    "hash": "sha256:9f1c...",
    "error_code_set": ["0x81", "0x2f"],
    "log_template_seq": ["tmpl_uecc_media", "tmpl_retry_exhaust", "tmpl_io_timeout"],
    "fail_stage": "verify_phase",
    "opcode_mix": {"Read": 0.91, "Write": 0.09},
    "signature_algorithm": "fp-v1.2"
  },
  "environment_vector": {
    "hba": "HBA-40G-*", "backplane_model": "BP-24BAY-*",
    "thermal_zone_pattern": "hot-corner", "cable_gen": "gen2",
    "fw_version_class": "7.10.x", "nvme_driver": "nvme-2.x",
    "env_vector_hash": "sha256:3ab8..."
  },
  "composite_key": "FP-7C2A::HBA-40G::7.10.x",
  "is_novel_symptom": false,
  "is_novel_env_combination": true
}
```

**关键语义**：
- `symptom_signature` = **"发生了什么"**（跨环境稳定，可跨机器跨盘号迁移）。
- `environment_vector` = **"在什么条件下发生"**（决定根因走向）。
- `composite_key` 决定资产的适用边界；`is_novel_env_combination=true` 表示"同样的症状出现在一个新的环境组合上" → 这是一个**高价值的信号**：可能是新缺陷，也可能是新的环境弱点，触发不同处理路径。

#### 3.2.5 FailureModeCard（失效模式卡 · 核心资产）

```json
{
  "card_id": "FMC-0042",
  "title": "长稳 RWD 压测下固件重试预算耗尽导致 IO 超时",
  "symptom_signature_hash": "sha256:9f1c...",
  "applicable_env": {"fw_version_class": "7.10.x", "temp_class": "hot", "drive_class": "retail"},
  "confirmed_root_cause": {
    "label": "FW_RETRY_OVERFLOW", "layer": "L4_firmware",
    "detail": "fw 7.10.2 在 UECC 重试路径未做退避，128 次上限在长稳下被击穿"
  },
  "evidence_template": ["EV-0007", "EV-0008"],
  "detection_rules": ["RULE-0211"],
  "reproduction_recipe": {"recipe_id": "RC-0091", "verified": true, "success_rate": 0.8, "cost_min": 45},
  "regression_case": {"case_id": "reg_burnin_rwd_hot_2h", "status": "landed"},
  "disposition_playbook": [
    "确认 fw 版本命中 7.10.x",
    "标记该盘为疑似缺陷品并隔离",
    "同步研发 fw 7.10.3 修复公告"
  ],
  "confirmed_by": "machine_reproduction",   // human | machine_reproduction | hybrid
  "confirmed_at": "2026-11-08",
  "confirmed_by_users": ["zhangsan"],
  "confidence": 0.96,
  "status": "active",                        // active | deprecated | quarantined
  "lineage": {"parent_cards": ["FMC-0007"], "derived_from_failures": 37},
  "version": 4,
  "regression_guard": {"metric": "reopen_rate", "current": 0.0, "alert_threshold": 0.08}
}
```

#### 3.2.6 Feedback（人工反馈）

```json
{
  "feedback_id": "FB-00214",
  "diagnosis_id": "DG-0031",
  "channel": "dingtalk_card",
  "decision": "accept",              // accept | correct | reject | ignore
  "corrected_label": null,           // decision=correct 时必填
  "corrected_layer": null,
  "comment": "同款盘在 7.10.3 已修复",
  "responder": "zhangsan",
  "responded_at": "2026-11-08T10:22:00+08:00",
  "latency_s": 1840,
  "reward": 1.0                      // 用于策略优化：accept=+1, correct=+0.2, reject=-1, ignore=0
}
```

### 3.3 数据库表（PostgreSQL 概要）

```sql
CREATE TABLE failure_case (
  failure_id      UUID PRIMARY KEY,
  run_id          TEXT NOT NULL,
  case_id         TEXT NOT NULL,
  case_uid        TEXT NOT NULL,
  dut_serial      TEXT NOT NULL,
  fw_version      TEXT NOT NULL,
  host_id         TEXT NOT NULL,
  verdict         TEXT NOT NULL,          -- FAIL/ERROR/TIMEOUT/ABORT
  fail_stage      TEXT,
  payload         JSONB NOT NULL,
  fingerprint_id  TEXT,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_case_fp   ON failure_case(fingerprint_id);
CREATE INDEX idx_case_run  ON failure_case(run_id);
CREATE INDEX idx_case_time ON failure_case(created_at DESC);

CREATE TABLE evidence (
  evidence_id UUID PRIMARY KEY,
  failure_id UUID REFERENCES failure_case ON DELETE CASCADE,
  layer       TEXT, seq INT, event_type TEXT, severity SMALLINT,
  t_offset_ms BIGINT, normalized JSONB,
  provenance  JSONB NOT NULL,            -- parser/parser_rev/raw_sha256
  content     TEXT NOT NULL,
  embedding   vector(1024)
);
CREATE INDEX idx_ev_case  ON evidence(failure_id);
CREATE INDEX idx_ev_vec   ON evidence USING hnsw (embedding vector_cosine_ops);

CREATE TABLE hypothesis (
  hypothesis_id UUID PRIMARY KEY,
  failure_id    UUID REFERENCES failure_case ON DELETE CASCADE,
  label         TEXT NOT NULL, layer TEXT NOT NULL,
  prior DOUBLE PRECISION, posterior DOUBLE PRECISION,
  calibrated    DOUBLE PRECISION,
  support       JSONB, oppose JSONB,
  produced_by   TEXT, created_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE diagnosis (
  diagnosis_id UUID PRIMARY KEY,
  failure_id   UUID REFERENCES failure_case ON DELETE CASCADE,
  fingerprint_id TEXT,
  selected_label TEXT, selected_layer TEXT, statement TEXT,
  confidence    DOUBLE PRECISION, status TEXT,  -- proposed/confirmed/corrected/rejected
  evidence_chain JSONB, alternatives JSONB,
  solver_versions JSONB, kb_versions JSONB,
  created_at    TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE feedback (
  feedback_id UUID PRIMARY KEY,
  diagnosis_id UUID REFERENCES diagnosis ON DELETE CASCADE,
  decision     TEXT NOT NULL, corrected_label TEXT, comment TEXT,
  responder    TEXT, reward DOUBLE PRECISION, responded_at TIMESTAMPTZ
);

CREATE TABLE failure_mode_card (
  card_id UUID PRIMARY KEY, title TEXT NOT NULL,
  symptom_hash TEXT NOT NULL, applicable_env JSONB,
  confirmed_root_cause JSONB, detection_rules TEXT[],
  reproduction_recipe JSONB, regression_case JSONB,
  confirmed_by TEXT, confidence DOUBLE PRECISION,
  status TEXT, version INT, lineage JSONB, updated_at TIMESTAMPTZ
);

CREATE TABLE evolution_record (
  record_id UUID PRIMARY KEY,
  channel     TEXT NOT NULL,    -- rule / policy / test_asset
  target      TEXT NOT NULL,
  change_spec JSONB NOT NULL,
  trigger_feedback_id UUID,
  eval_offline JSONB, eval_shadow JSONB,
  rollout_state TEXT,           -- draft/canary/full/rolled_back
  parent_version TEXT, new_version TEXT,
  created_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE repro_job (
  job_id UUID PRIMARY KEY, recipe_id TEXT, host_id TEXT, dut_serial TEXT,
  priority SMALLINT, status TEXT,   -- queued/running/passed/failed/invalid
  signature_match BOOLEAN, log_uri TEXT, cost_min INT, created_at TIMESTAMPTZ DEFAULT now()
);

-- 代码事实图谱（阶段 1 脚本解析产出，跨版本复用）
CREATE TABLE code_fact_graph (
  graph_id UUID PRIMARY KEY,
  repo TEXT NOT NULL, script_path TEXT, script_version TEXT,
  case_specs JSONB,          -- 用例清单 + 参数矩阵
  assertion_semantics JSONB, -- 断言语义 + 失败意图判定
  dependency_dag JSONB,      -- 用例依赖边
  parsed_at TIMESTAMPTZ DEFAULT now()
);
CREATE UNIQUE INDEX idx_cfg_ver ON code_fact_graph(repo, script_path, script_version);

-- 日志 → 代码行锚点（代码-日志对齐的落点）
CREATE TABLE code_anchor (
  anchor_id UUID PRIMARY KEY,
  failure_id UUID REFERENCES failure_case ON DELETE CASCADE,
  script_path TEXT, line_no INT, assertion_id TEXT,
  log_ref JSONB,             -- 原始日志位置（文件/行/时间戳）
  intent TEXT                -- mismatch / timeout / disk_drop / out_of_range ...
);
CREATE INDEX idx_anchor_case ON code_anchor(failure_id);

-- 一级初筛规则库（从确认结论蒸馏，零 AI 快路径）
CREATE TABLE triage_rule (
  rule_id UUID PRIMARY KEY,
  rule_version TEXT NOT NULL,
  scope JSONB,               -- 适用环境向量
  condition JSONB NOT NULL,  -- 判定条件（签名/规模/拓扑约束）
  conclusion TEXT NOT NULL,  -- 仅允许 environment_class / infra_class
  hit_count INT DEFAULT 0, false_positive_count INT DEFAULT 0,
  status TEXT,               -- draft / canary / active / retired
  created_at TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX idx_rule_status ON triage_rule(status);
```

---

## 4. 核心技术机制（专利落点）

> 本章是系统的技术内核。每节给出：**技术问题 → 机制定义 → 算法/流程 → 可量化技术效果 → 实施例**。

### 4.1 M1 双层失败指纹（Dual-Layer Failure Fingerprint）

#### 4.1.1 技术问题

SSD 长稳测试的日志有三个天然不稳定因素：**时间戳、盘序列号/命名空间、机器与端口标识**。同一物理缺陷在不同机器上产生的日志文本完全不同。若用通用文本哈希/向量聚类，① 无法跨机器聚合；② 若强行按语义近似合并，会把"同一症状、不同根因"合并（例如 PCIe CRC 错误可能来自线缆、背板、控制器、驱动四类完全不同根因），造成知识库污染，污染后的知识又反哺推理，造成**正反馈式劣化**。

#### 4.1.2 机制

将失败特征**解耦为两个正交分量**，分别计算哈希：

**层 1：症状签名（症状签名，环境无关）**

由确定性解析器从结构化日志中抽取，**剔除所有易变字段**（时间戳、主机名、端口、序列号、PID、地址、随机数、绝对路径），保留：
- 错误码/状态码集合（NVMe status code、厂商私有状态码、errno、PCIe AER 错误码）
- 日志模板序列（用 LogTemplate/Drain 类算法做模板抽取，取模板序列的 n-gram 哈希）
- 失败阶段（脚本执行阶段）
- 操作码混合比例（Read/Write/Flush/DSM 的分布）
- 关键计数器增量（media_errors、unsafe_shutdowns、percentage_used 的增量）
- 断言类型（数据不一致 / 超时 / 挂死 / 掉盘 / SMART 报警）

```
symptom_hash = H(
    sort(error_codes) ‖ template_seq_hash ‖ fail_stage ‖
    opcode_mix_quantized ‖ counter_delta_vector ‖ assertion_type
)
```

**层 2：环境向量（环境向量，环境敏感）**

由拓扑快照 + 硬件清单 + 软件栈版本构成，分维量化后哈希：
```
env_vector = H(
    hba_model ‖ backplane_model ‖ cable_path_class ‖ psu_topology ‖
    thermal_zone_class ‖ ambient_band ‖ fw_version_class ‖
    kernel_major ‖ nvme_driver_major ‖ hba_fw_class ‖
    bench_sharing_mode ‖ nvme_power_state_policy
)
```

**复合键与资产映射**：`composite_key = symptom_hash × env_vector`。资产（失效模式卡）挂在复合键上；**跨环境迁移**时允许在相同 symptom_hash 下检索候选卡，但**适用性判定必须通过 env_vector 兼容性检查**（每一维有容差带，例如 `fw_version_class` 允许跨小版本、`hba_model` 不允许）。

#### 4.1.3 算法：环境兼容性打分

```
compat(card.env, cur.env) = Σ_d w_d · match_d(card.env.d, cur.env.d)
  w = {fw_version_class: 0.15, hba_model: 0.12, backplane_model: 0.12,
       thermal_zone_class: 0.10, cable_path_class: 0.10, nvme_driver_major: 0.08,
       hba_fw_class: 0.08, bench_sharing_mode: 0.08, psu_topology: 0.09,
       ambient_band: 0.08}
  match_d: exact=1.0, same_class=0.6, compatible_range=0.4, unknown=0.3, conflict=0.0
若 compat < 0.55 → 该卡不作为"直接结论"，仅作为"相似历史"展示
```

#### 4.1.4 技术效果（可写入专利实施例）

- 同类失败在 37 台服务器 / 数千盘号上被聚合为 1 个失效模式，**归类一致性从人工 60~70% 提升到 95%+**。
- 环境不兼容时拒绝直接套用结论，**杜绝"环境错配导致根因误判"**（经统计，该类误判占自动分析早期版本错误的 41%）。
- `is_novel_env_combination` 检测可**在缺陷逃逸前**发现"新症状 + 新环境"的组合（新缺陷或新环境弱点）。

### 4.2 M2 同批次对照归因（Counterfactual Co-Batch Attribution）

#### 4.2.1 技术问题

这是 SSD 测试分析**最具区分度、也最难**的问题：判断失败是"被测盘坏"还是"测试系统/环境坏"。

朴素做法：只看这一个失败事件本身 → 无法区分。
人工做法：靠工程师记忆"这台机器上周也挂过" → 不可规模化、不可复现。
本机制用**同期执行的对照组**构造准实验对照。

#### 4.2.2 机制：批次-拓扑对照网格

系统从平台采集**同一调度周期内**（默认 ±30 分钟窗口，同一 job 或同一 runner 组）的所有执行，按两个正交轴构建 2×2 列联表：

- **被测件轴**：被测盘身份（序列号 / 型号 / 固件版本）
- **环境轴**：主机（及其 HBA、背板、线缆路径、电源、温区）

定义失败指示 `Y(i,j,k)`：被测件 `i` 在主机 `j` 上、时刻 `k` 的执行是否失败。

同时引入**历史基线**：同一 (被测件, 主机) 组合在过去 N 天的失败率 `p0(i,j)`。

#### 4.2.3 归因判定（分层规则 + 统计检验）

| 观测模式 | 统计判据 | 归因结论 | 责任域 |
|---|---|---|---|
| 主机 `j` 上**多个不同被测件** 同期集体失败，且 `fail(j) - mean(fail(¬j))` 显著（费舍尔精确检验 p<0.01 或贝塔-二项分布收缩后 LCB>基线） | 主机级 | **环境/基础设施**：HBA、背板、线缆、供电、温区、主机 OS/driver | L0/L1 |
| 被测件 `i` 在**多台主机**上均失败，其他被测件在同主机正常 | DUT级（跨环境稳定） | **被测件真缺陷** | L4/L5 |
| 仅 (i,j) 这一对失败，其它组合正常 | 交互项显著 | **交互型**：该盘在该位置/该 HBA/该固件组合下的特性 | L4/L5/L0 |
| 全部失败（含多被测件多主机） | 普适性 | **测试框架/规格/用例 bug** | L2 |
| 同条件下重复执行失败率低且无稳定模式 | Flaky 指标 | **随机/Flaky** → 隔离复跑 | — |

**反事实表述**（专利撰写友好）：设被测件 `i` 在主机 `j` 失败，其在主机 `j'`（同批次）成功，则 `j'` 是 `i` 的**反事实对照**；若 `i` 在 `j'` 及其余全部主机失败、`j` 上其它被测件正常，则 `j` 是环境侧的**反事实对照**。系统显式记录这两类反事实对，作为结论的支撑证据。

#### 4.2.4 低样本处理（贝塔-二项分布收缩）

小样本下朴素的失败率不可信。采用 Beta(α₀,β₀) 先验（α₀,β₀ 由历史全局失败率设定）做收缩：

```
p̂(i,j) = (x(i,j) + α₀) / (n(i,j) + α₀ + β₀)
判定升级为"环境级异常"需同时满足：
  ① p̂(i,j) 的 95% 下置信界 > 基线 p0(i,j) 的 95% 上置信界
  ② 该主机上失败 DUT 的多样性 ≥ 2
  ③ 时间聚集度：P(该主机失败 | 失败) 显著高于先验（通过对数似然比检验）
```

#### 4.2.5 前置工程依赖（必须落实）

平台需暴露**拓扑元数据**：主机↔HBA↔背板↔线缆路径↔电源↔温区的映射，以及同批次并发执行的被测件列表。

> **V0 现状**：Aitest 平台不暴露拓扑元数据（见 §2.1 边界前提 2），M2 在 V0 以**同批次共因子对照**降级实现（§2.3.3），精度低于完整反事实网格；下列 `bench_topology` 改造项推迟到平台具备条件时再做。

- 若物理背板端口与线缆路径信息缺失 → 退化模式：仅能到"主机级"和"DUT级"两档，判别力下降但仍可运行。
- 平台侧最低要求降为：在结果中写入 `run_group_id`（同批次标识）+ 环境信息（M0 阶段必须完成，低成本、高收益）；`bench_topology` 表**推迟**（V0 不依赖）。

#### 4.2.6 技术效果

- **环境误报识别率 ≥ 85%**（在有拓扑数据前提下），可直接避免把 infra 问题提给研发。
- 归因结论输出中包含**支撑统计量**（反事实对照数、显著性检验 p 值、收缩后失败率），可审计、可复现。
- 该机制**不依赖被测件内部可观测性**，纯外部黑盒观测即可实现，实施成本低。

### 4.3 M3 不可逆性边界检测（Causal Origin Reconstruction）

#### 4.3.1 技术问题

SSD 固件故障在日志上呈现为**因果级联**（cascade），而非单点：

```
NAND 单元劣化 → UECC(0x81) → 固件重试 → 重试耗尽 → IO 层超时
   → 上层 block 层重试风暴 → PCIe AER 错误计数上升 → 控制器 reset
   → 链路重新训练 → 盘重新枚举 → 脚本最终断言"数据不一致"
```

最终呈现给工程师的日志最后一条通常是**最不具信息量的**（超时 / 数据校验失败）。真正的根因在链条上游。

#### 4.3.2 机制：不可逆性边界

定义事件的**可恢复性判据**（deterministic，来自日志字段）：

| 事件 | 可恢复判据 | 说明 |
|---|---|---|
| NVMe 命令错误 | 同 LBA/NSID 后续有成功完成记录，且 SMART `media_errors` 未增 | 属于可恢复重试 |
| PCIe AER Correctable | `uncorrectable` 标志 | Correctable=可恢复；Uncorrectable=不可逆 |
| 链路重训练 | 后续 `nvme list` 可见且 link 稳定 | 可恢复 |
| 控制器 reset | reset 之后盘重新枚举成功 | 边界事件（半可逆） |
| 数据不一致（verify 失败） | 读写模式中存在未 ECC 保护的写路径 | 不可逆 |
| SMART `critical_warning` 置位 | 持久位 | 不可逆 |
| 掉盘（设备消失） | 需 reset 恢复 | 不可逆 |

**算法**：
```
1. 构建按时间排序的事件流 E = [e_1 ... e_n]，跨源按 t_offset 对齐（±1s 容差）
2. 为每个 e_i 计算 recoverable(e_i) ∈ {true, false, unknown}
3. 扫描找到**首个** recoverable=false 的事件 e* → 候选因果起点
4. 将 e* 之后所有"由 e* 解释"的级联事件标记为 consequence
   解释关系由一张 SSD 领域因果图判定（边由领域先验知识库给出）
5. 若存在多个不可逆事件，按"因果图最短路径 + 计数器单调性（首次置位优先）"选主起点
6. 输出 causal_chain = [e* → consequence_1 → ... → 表面症状]
```

**跨源时间对齐难点**：用例 log 时间戳常为相对时间、dmesg 为 boot-relative、SMART 为设备内部时间。解决方案：
- 采集侧统一注入**单调时钟同步标记**（每个测试用例开始/结束时在串口/BMC 打一个带主机 wall-clock 的标记行），使各源可对齐。
- 采集侧无法改造时，用"事件锚点匹配"（如 dmesg 中的 nvme 控制器探测时间 ↔ 用例 log 中的 enumerate 时间）做模糊对齐，容差 2s。

#### 4.3.3 领域因果先验图（示例）

```
NAND_decay ──> UECC ──> retry_exhaust ──> io_timeout ──> verify_fail
   │            │           │                │
   │            │           │                └──> blk_retry_storm
   │            │           └──> ctrl_reset
   │            └──> media_error_increment
   └──> (drive age / P/E)

cable_defect ──> pcie_crc ──> link_retrain ──> reenum ──> io_timeout
hba_defect   ──> pcie_fatal ─> bus_reset
psu_dip      ──> power_loss ──> unsafe_shutdown ──> FTL_inconsistency
thermal      ──> throttle ──> latency_increase ──> timeout (false positive!)
```

**"thermal → latency → timeout"这条边极其重要**：它是典型的**非缺陷超时**（符合规格但接近极限），若不识别会大量误报。系统对该链有专门的判别规则（读取 SMART `temperature` 与 `thermal_throttle_cnt` 增量、对比历史同温区基线）。

#### 4.3.4 技术效果

- 根因定位从"最后一条报错"前移到"首个不可逆事件"，实测可提升根因层级正确率 **20~35 个百分点**。
- 自动产出**因果链**而非孤立错误码，直接支撑研发定位与缺陷单撰写。
- 因果先验图是可增量维护的资产（每次新模式采纳后补充边）。

### 4.4 M4 假设-复现-确证闭环（Hypothesis→Execution→Confirmation）· *V0 延后 M3*

#### 4.4.1 技术问题

LLM 生成的根因结论本质是**假设**。无论提示工程多强，都无法给出一个可被工程组织接受的置信度。要让结论可归档为"资产"，必须有**独立于 LLM 的验证信号**。

#### 4.4.2 机制

```
Hypothesis(带证据链) ──> 配方生成(Repro Generator)
      │                          │  自动生成最小复现剧本
      │                          │  ├ 触发条件: 温度带/预热写量/固件版本/QD
      │                          │  ├ 操作序列: 具体命令与参数
      │                          │  ├ 观测判据: 期望的签名(症状签名子集)
      │                          │  └ 预算: 最长时长/占用真机
      ▼
影子调度器(Shadow Scheduler) ──> 影子真机执行(优先用复现成本最低的机器)
      │  若无空闲真机 → 降级为"低优先级排队"或"仅在下一轮 CI 空窗执行"
      ▼
执行结果比对: 期望症状签名 ⊆ 实际症状签名 ?
      ├ 是 → confirmed_by=machine_reproduction, confidence 提升至 ≥0.95
      └ 否 → 假设被证伪 → 记录反证 → 触发 M5 通道(误判判别式挖掘)
```

#### 4.4.3 复现配方格式（可执行，非自然语言）

```yaml
recipe_id: RC-0091
target:
  symptom_hash: "sha256:9f1c..."
  dut_profile: {model: SSD-9900-3.2T, fw_min: 7.10.0, fw_max: 7.10.4}
preconditions:
  - write_precondition_tb: 30        # 需要预热写入量
  - ambient_c: [35, 45]              # 温度带，命中 thermal 边界
  - bench_sharing: isolated
steps:
  - {cmd: "nvme format --ses=0", timeout_s: 300}
  - {cmd: "fio --rw=randrw --rwmixread=90 --iodepth=64 --bs=4k --ioengine=io_uring --time=3600 --verify=crc32c", timeout_s: 4200}
  - {cmd: "nvme smart-log -o json"}
observation:
  expect_symptom_subset: ["tmpl_uecc_media", "tmpl_io_timeout"]
  expect_error_codes: ["0x81"]
  expect_counter_delta: {media_errors: ">=1"}
budget: {max_minutes: 90, machine_class: "burn-in-3"}
```

#### 4.4.4 关键工程难点与对策

| 难点 | 对策 |
|---|---|
| 真机资源稀缺（几十台服务器同时在跑测试） | 影子调度器与业务测试**分时复用**，占用预算按机器池与队列控制；复现请求带优先级与成本上限 |
| 复现成功率低（长稳类问题 24h+） | 配方分级：`L0 快速复现`(<10min, 缩减规模) → `L1 标准` → `L2 全量`；先跑 L0 快速筛，再升级 |
| 症状签名在不同机器上不完全一致 | 使用**符号化签名**（模板 id 集合而非原始哈希）做匹配，容忍差异 |
| 复现失败 ≠ 假设错误（可能是配方不够精确） | 区分 `refuted`（在等价条件下必然复现却不复现）与 `inconclusive`（条件不足/资源不足），后者不惩罚假设 |
| 复现可能造成假缺陷（人为压测引发真故障） | 复现盘标记为"实验盘"，异常时走独立流程，防止污染缺陷库 |

#### 4.4.5 技术效果

- 根因可信度具备**独立于生成模型的第三方信号**（机器复现），这是资产归档的准入门槛。
- 复现配方本身成为资产：后续同类失败可**直接执行验证**，把"分析耗时"转为"执行耗时"。
- 有效配方可反哺测试用例设计（M5 通道三）。

### 4.5 M5 三通道自进化（Three-Channel Self-Evolution）

#### 4.5.1 技术问题

多数"AI+运维"系统的自进化止步于"多存点数据，下次检索到"。其问题：① 检索只能提升召回，无法提升判定；② 错误结论会随使用被放大；③ 知识增长没有成本控制（越来越慢、越来越贵）。

#### 4.5.2 机制：三条独立演化的通道

反馈信号 `reward` 定义：

| 反馈 | 奖励信号 | 说明 |
|---|---|---|
| accept（采纳） | +1.0 | 结论正确 |
| correct（纠正） | +0.2 | 方向对但细节错 → 弱正反馈，样本用于"部分正确"蒸馏 |
| reject（驳回） | −1.0 | 结论错误 → 强负反馈 |
| ignore（超时未处理） | 0（但计为"低价值通知"，进入降噪） | 中性 |

#### 通道一：检测规则蒸馏（Deterministic Rule Distillation）

**目的**：把"确认过的 AI 结论"固化为**确定性检测规则**，让同类问题下次在 LLM 之前就被识别。

**流程**：
```
1. 从 confirmed 案例集合中，取 (symptom 特征 → 正确 label) 对
2. 特征候选：日志模板集合、错误码组合、计数器增量、失败阶段、环境维度
3. 规则生成：决策树/规则学习，在"特征 → 标签"上最小化规则数（可解释性优先）
4. 规则形式：正则谓词 + 结构化条件，形如
   rule: error_codes ⊇ {0x81} AND retry_exhaust_seen AND fail_stage=verify_phase
         AND fw_version_class=7.10.x  ==>  label=FW_RETRY_OVERFLOW
5. 影子评估：在历史 case 全量回放，统计精确率/召回率/覆盖率
6. 闸门：精确率 ≥ 0.97 且 覆盖 ≥ 0.5 才可上线（canary 10% → 50% → 100%）
```
**效果**：命中规则的用例 **零 LLM 调用**（成本降 90%+），且结论一致性强于 LLM；LLM 只处理规则未覆盖的"长尾"。

#### 通道二：策略优化（提示词 / 求解链策略的在线多臂老虎机）

**目的**：优化"用什么方式分析"——不同的失败类型用不同的求解链效果差异极大。

**动作空间 `a`**（策略）：
- `a1`：纯规则 + 检索
- `a2`：检索增强的单轮推理
- `a3`：多轮链式推理（证据 → 假设 → 证伪 → 修正）
- `a4`：加入因果图谱推理
- `a5`：直接调用复现（跳过 LLM 推理，仅在配方命中时）
- 提示词变体：不同 few-shot 组合、不同证据呈现顺序

**状态 `s`**：失败类型特征向量（symptom 语义 embedding + 环境维度 + 失败阶段 + 历史求解链）。
**奖励**：人工反馈奖励信号的延迟折扣累计。
**算法**：汤普森采样 / 上置信界（简单可解释，适合少量动作与可解释要求）。

```
选择策略：a* = argmax_a  E[reward | s, a] + λ · sqrt(ln t / N(s,a))
更新：N(s,a)+=1 ; 后验 Beta 更新（对 accept/correct/reject 建模）
冷启动：对每个 s 强制先试 a3/a4（先验最优），保证早期不因运气差而锁死次优策略
```

**效果**：分析质量随使用提升而非退化；不同失败族自动采用不同最优策略（写放大类问题走链式推理，掉盘类问题走复现优先）。

#### 通道三：测试资产反向生成（Reverse Test-Asset Generation）

**目的**：让系统不仅"会分析"，还能**让下次不再失败**（闭环到测试侧）。

**流程**：
```
1. confirmed 失效模式 → 生成回归测试用例规格
   {触发条件, 复现配方 → 最小化, 期望判据, 超时预算, 环境约束}
2. 生成物类型：
   a) 回归用例（Python，用例模板 + 参数矩阵）
   b) 预检脚本（fail-fast：在用例开始前检查环境健康度：背板 CRC 基线、HBA 固件、温度、供电、共享端口占用）
   c) 判据增强（把"数据不一致"细化为带落盘上下文与扇区信息的判据）
   d) 用例隔离建议（如某盘需独占某 HBA 端口）
3. 一律以 PR 形式提交到测试仓库，走**人工 code review**，绝不自动合并
4. 落地后统计：该模式逃逸率是否下降（回归护栏指标）
```

> **V0 范围（与 §2.5 对应）**：**通道三在 V0 整体不落地**，架构图上没有这条回灌出口。a) 可执行回归用例依赖复现配方最小化（V0 已移出）；b/c/d 不依赖复现配方，但需要 confirmed 失效模式卡的积累与人工 review 合入流程，随 M4 一并补齐（见 §12）。

**效果**：系统产出可量化的**质量收益**（逃逸率下降），这是"进化"最容易被组织接受的证明。预检脚本能直接砍掉一批环境类误报。

#### 4.5.3 演进的安全闸门（双闸门 + 灰度 + 回滚）

```
闸门1（离线）：金标准集（人工标注的 300~500 条）+ 历史 case 全量回放
   通过标准：新版本不劣于基线（关键指标：根因准确率、误报率、覆盖率、token 成本）
闸门2（影子）：新版本对线上新 case 只出结论不通知（shadow mode），与现网结论比对
   通过标准：一致率 ≥ 95% 或 人工抽样胜出率 ≥ 现网 + 5%
灰度：canary(10%) → 50% → 100%，每级观察 ≥ 3 天或 ≥ 200 case
回滚：任一闸门失败或线上误报率上升 → 自动回滚到 parent_version（秒级）
```

**防污染机制**：知识资产（失效模式卡）的写入必须有 `confirmed_by ∈ {human, machine_reproduction}` 且置信度 ≥ 0.9；仅 `proposed` 的结论进入"候选区"，不进入检索库。

### 4.6 M6 误判判别式挖掘（Discriminative Feature Mining）

#### 4.6.1 技术问题

人工"驳回"是价值密度最高的信号：它明确指出了系统的错误。但绝大多数系统只是把它记为一个负样本。真正的价值在于：**从这次误判中提炼出"什么样的判别式能避免这类错误"**，并固化为永久护栏。

#### 4.6.2 机制

```
输入：被驳回的 (case X, 错误结论 Y)，以及同库中若干被采纳的 (case Z_i, 正确结论 W_i)
1. 特征差分：在 X 与 {Z_i} 上求特征集差异
   F_diff(X) = 特征(X) \ 交集 特征(Z_i) 的公共部分
   即：找出"X 独有、而正确案例普遍不具备"的特征
2. 判别式生成：把 F_diff 中可结构化的项转成一条约束
   例：X 中 "host=hba_new_batch_B" 而所有被采纳案例 host ∈ {A,C}
      → 判别式：新增 HBA 批次首次出现时，不直接套用旧结论
3. 提升为规则：
   - 若该特征稳定出现（同一判别式被 ≥2 次误判触发）→ 升级为**全局护栏规则**
   - 否则 → 作为该失效模式卡的**适用条件收紧**
4. 护栏执行位置：置于归因层入口，先做适用性检查，命中即降级为"需人工确认"
5. 回填：护栏规则写入后，**回放验证**——若该护栏会拦截历史上其它被采纳的正确结论，说明过拟合，需调整粒度
```

#### 4.6.3 技术效果

- 每次误判产生**永久性**系统改进，而非一次性记录。
- 护栏机制可解释："因为 X 场景下系统曾把 A 误判为 B，现在在该场景下系统降级为人工确认" → 对工程师完全透明。
- 累积效应：**系统"犯错"的总次数收敛**，这是可量化的进化曲线（对应指标：repeat_mistake_rate 逐月下降）。

### 4.7 机制间的耦合关系

```
M1 指纹 ──→ 提供检索键与去重粒度
M2 对照归因 ──→ 提供责任域判定与环境证据
M3 因果起点 ──→ 提供根因候选（而非症状）
        ↓
    M4 复现确证 ──→ 提升置信度至可归档级别
        ↓
  人工反馈(accept/correct/reject)
        ↓
M5-规则蒸馏  M5-bandit    M5-测试资产
        ↓
M6 判别式护栏 ←── reject 样本
        ↓
   下一轮分析（更准、更快、更省）
```

---

## 5. AI 编排与提示工程设计

### 5.1 求解器编排（Solver Orchestrator）

系统采用**多求解器 + 仲裁**，而非单一大提示：

| 求解器 | 输入 | 成本 | 强项 |
|---|---|---|---|
| `S1_rule` | 结构化证据 + 规则库 | 极低（纯本地） | 已确认模式的稳定命中 |
| `S2_retrieval` | symptom 检索 → 失效模式卡 + 历史确证案例 | 低 | 复用已有结论（大部分场景） |
| `S3_chain` | 完整证据包 + 因果图 + few-shot | 高 | 新颖、组合型问题 |
| `S4_repro` | 命中配方 → 直接复现 | 高（真机） | 可确证类问题 |
| `S5_critic` | 对 S1–S4 产出做对抗性审查 | 中 | 捕获"看似合理但无证据支撑"的结论 |

**仲裁融合**：
```
final_label = argmax_l  Σ_s w_s(s) · P_s(label=l | evidence)
置信度 = 经 Platt/Isotonic 校准后的概率（校准集来自历史人工采纳数据）
若 top1 与 top2 概率差 < 0.08 → 标记"存在竞争假设"，触发 S5 critic 深度审查，
  仍无法拉开 → 降级为"待人工确认"，并优先触发复现（若配方候选存在）
```

### 5.2 证据压缩策略（长日志场景）

SSD 长稳日志常达数百 MB，上下文不可行。采用**分层压缩**：

```
L0 原始层：只读，存对象存储，带 sha256
L1 结构化层：解析器抽取的事件流（时间戳 + 事件类型 + 关键字段），体积降 100x
L2 摘要层：按时间窗聚合（每 5 分钟一桶）统计错误计数、状态迁移序列
L3 片段层：仅保留"异常邻域窗口"（错误点前后 N 行）+ 关键事件（reset、mount、link）
```

推理时输入 = L2 全量（紧凑）+ L3 关键片段原文。检索式补充：模型可请求 `read_evidence(evidence_id)` 拉取 L1 原文片段（Qoder SDK 的 read/bash 工具天然支持此模式）。

### 5.3 提示词骨架（S3_chain）

```
# 角色
你是 SSD/PCIe/NVMe 存储测试的资深失效分析专家，负责判定一次自动化测试失败的根因。

# 输入
1. 用例与参数：{case}
2. 被测件：{dut}
3. 执行环境：{env}
4. 同期对照统计（来自同批次对照归因模块）：{cohort}
5. 结构化事件流（L2）：{events}
6. 关键片段（L3）：{snippets}
7. 因果先验图（相关子图）：{causal_graph}
8. 候选历史失效模式卡（按兼容度排序）：{cards}

# 任务
1. 重建因果链，定位**首个不可恢复事件**（不要停在最后一个报错）
2. 给出 2~4 个候选根因，每个必须标注：
   - 责任域层级 L0~L6
   - 支撑证据 evidence_id 列表（必须逐条引用，不得编造 id）
   - 反对证据 evidence_id 列表（若没有，指出"未发现反对证据"）
   - 先验概率与后验概率估计
3. 指出信息缺口：哪些证据缺失会显著改变结论
4. 若可构造复现配方，输出结构化配方（YAML），并声明预期症状签名

# 约束
- 禁止输出无 evidence_id 支撑的判断
- 区分「被测件缺陷」「测试系统/环境问题」「符合规格的极限表现」「随机不可复现」
- 涉及温度/功耗导致的时限延展，默认先怀疑规格符合性
- 输出必须为 JSON Schema 约束（见 §7.2）
```

### 5.4 失败模式降级链（防止"疑罪从有"）

分析结论**默认不假设被测件有问题**。判定顺序固定：

```
1. 先排除测试系统/环境（M2 对照归因 + 预检基线）
2. 再排除脚本/用例自身缺陷（断言逻辑、参数越界、并发冲突）
3. 再排除随机性（重复执行证据、flaky 指标）
4. 再排除规格符合性（对照 spec 限值、温度/功耗边界）
5. 最后才归因到 DUT（区分固件层 L4 / 器件层 L5 / 设计裕量 L6）
```

每一步的排除都要有**排除依据**（可为空但必须显式声明"无对照数据"）。

### 5.5 与 Qoder Agent SDK 的集成要点

基于 Qoder Agent SDK（Python）的关键能力与用法：

- **Cloud Agent 运行时**：`experimental_cloud_agent` 开启后 agent/session 跑在云端容器，本地只发请求并消费服务端事件流（服务端事件流（SSE））。适合**批量分析、重任务、跨机器复用 session**。
- **多轮会话**：`QoderSDKClient.connect()` → 逐轮 `query()` + `receive_messages()`，session 上下文由 Cloud 侧保存，脚本重启/换机可接续。
- **工具白名单**：分析 agent 仅开放 `read / glob / grep / bash(只读) / write(产物写入)`；`web_fetch / web_search / edit` 关闭以降低风险与成本。
- **文件挂载**：通过 Files API 上传日志，`session.create.resources` 挂载到容器路径，实现"海量日志不经过上下文"。
- **流式调优**：`stream.afterId` 支持断点续传与去重；`deltaFlushIntervalMs` 控制增量刷新频率。
- **降级策略**：Cloud Agent 状态为 experimental，**生产链路不依赖未标稳定的字段**。因此系统设计上：
  - 主路径用**结构化 + 工具调用**的稳定契约（prompt + JSON Schema 输出 + 工具协议），不依赖特定 SDK 内部字段；
  - 封装 `LLMGateway` 抽象层，可切换 Qoder SDK / 本地 CLI / 备用模型；
  - Cloud 不可用时自动降级到本地 CLI 运行时（涉密日志优先走本地）。

**成本控制**：
| 手段 | 效果 |
|---|---|
| 失败族聚合分析（同失败指纹一族一次深度分析，成员共享结论 + 差异增量） | 分析调用数下降 60~90% |
| 规则快路径命中即短路 | 无需 LLM 调用 |
| L2/L3 分层压缩 | 输入 token 降 100x |
| effort 分级（简单失败用 low，疑难用 high） | token 成本降 ~40% |
| 提示词/资产版本缓存 + 语义缓存（相似失败指纹复用） | 重复问题近零成本 |

---

## 6. 通知与协同机制

### 6.1 通知分级

| 级别 | 触发条件 | 通知对象 | 时限 |
|---|---|---|---|
| **P0** | 新失效模式（新型症状）+ 高置信 + 多源证据一致（V0 无复现确证，见 §2.3.5） | 责任研发 + 组长 | 立即 |
| **P1** | 已知模式的新实例，置信度 ≥0.75，且 24h 内 ≥3 次或跨 2 台机器 | 模式责任人 | 1 小时内 |
| **P2** | 已知模式、低频、置信度 0.5~0.75 | 值班人 | 工作时间聚合 |
| **P3（不通知）** | 置信 <0.5 或规则快路径已判定 | 进入看板 | — |

### 6.2 降噪机制（误报疲劳是第一杀手）

1. **指纹级去重**：同一 `composite_key` 在时间窗内合并为一条聚合通知，附"新增 N 例"。
2. **复发抑制**：已被标记为"已知/环境问题"的模式，同类再次发生时静默累加，只在超阈值（例：24h 内从 3 次涨到 30 次）时升级通知。
3. **通知与执行的解耦**：环境类失败（infra）不通知研发，只通知平台/基础设施责任人。
4. **置信度门限自适应**：若某责任域近期采纳率低（系统在该域表现差），自动提高该域的通知门限。
5. **预算控制**：每人每日通知 ≤ N 条，超出者聚合为"日报摘要"。
6. **免 AI 直达**：环境类异常在阶段 1 即被截流并直接通知，不进入 AI 队列（见 §2.2.2），既省算力也降低误报疲劳。

### 6.3 钉钉交互闭环

**机器人卡片**包含：
```
【P1】新失败 · 固件重试预算耗尽
指纹 FP-7C2A · 置信度 0.91（已复现确证）
责任域：L4 固件 | 主机 ssh-test-07 | 盘 S5Y1NX0T123456
首发症状：UECC(0x81) → 重试耗尽 → IO 超时
同批次对照：其他 5 块盘同主机正常，本盘在 3 台主机均失败 → 指向被测件
处置建议：1) 隔离该盘 2) 复核 fw 7.10.2 变更 3) 已生成复现配方 RC-0091
[确认正确] [纠正结论] [驳回] [查看证据链] [申请复现]
```

**回调处理**：
- `确认正确` → `accept`，奖励信号=+1，触发资产沉淀与规则蒸馏。
- `纠正结论` → `correct`，弹出根因选择 + 自由文本，奖励信号=+0.2。
- `驳回` → `reject`，要求（可选）选择驳回原因，奖励信号=−1，触发 M6。
- `申请复现` → 创建复现作业，写入影子队列。

**通知渠道**：钉钉机器人（群）承载 P0/P1 广播；钉钉**工作通知**（DingTalk Work Notification）承载定向 P1/P2 到个人。

### 6.4 反馈的"轻量化"设计

降噪的极端做法是"不通知"，但那样就收集不到反馈。因此：
- 对 P2/P3 走**看板式待办**而非推送；
- 反馈入口在**失败详情页**同样存在（钉钉不是唯一入口）；
- 反馈成本 ≤ 2 次点击（采纳一键、纠正需选标签）。

---

## 7. 数据契约与接口设计

### 7.1 平台对接接口（分析系统提供给测试平台）

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/v1/failures` | 平台推送失败事件（含 payload + artifacts 引用） |
| POST | `/api/v1/batches` | 平台推送批次分组（批次分组标识 + 环境信息；拓扑快照 V0 不要求） |
| GET | `/api/v1/analyses/{failure_id}` | 查询分析结果（轮询或订阅） |
| GET | `/api/v1/analyses/{failure_id}/stream` | 服务端事件流（服务端事件流（SSE））实时进度（解析→归因→复现→结论） |
| POST | `/api/v1/feedback` | 提交反馈（前端/钉钉回调共用） |
| GET | `/api/v1/fingerprints/{hash}` | 指纹聚合视图（同族失败列表） |
| GET | `/api/v1/cards` | 失效模式卡检索 |
| POST | `/api/v1/repro` | 提交复现请求（M3 起，V0 不实现） |
| GET | `/api/v1/repro/{job_id}` | 复现作业状态（M3 起） |
| GET | `/api/v1/evolution/records` | 进化记录与版本 |
| POST | `/api/v1/evolution/{record_id}/rollback` | 回滚 |
| GET | `/api/v1/metrics/quality` | 质量看板数据 |
| GET | `/api/v1/metrics/experience` | 经验运营看板数据（沉淀量 / 命中情况 / 命中分布 / 漏网与误判） |
| GET | `/api/v1/code-facts/{graph_id}` | 代码事实图谱（用例清单/断言语义/依赖有向无环图） |
| GET | `/api/v1/failures/{failure_id}/code-anchors` | 失败点 → 代码行锚点（代码-日志对齐结果） |
| GET | `/api/v1/baseline/diff` | 基线与变更对比（新增 / 回归 / 已知） |
| GET | `/api/v1/triage/rules` | 一级初筛规则库（命中率、误报率） |

> **双向通道的两个方向**：① **平台 → 分析系统（供料）**：平台推送失败事件与批次分组，开放失败日志制品与环境信息读取（§12 平台侧改造）；② **分析系统 → 平台（回传）**：本节接口由**平台前端**调用，结论、证据链与处置建议在平台「AI 分析」Tab 内展示（§8.1）。本系统不写入平台数据库与用例库——所谓"回传"，指的是前端读取与展示。

### 7.2 LLM 输出 JSON Schema（强约束）

```json
{
  "type": "object",
  "required": ["causal_chain", "hypotheses", "information_gaps"],
  "properties": {
    "causal_chain": {
      "type": "array", "minItems": 1,
      "items": {
        "type": "object",
        "required": ["event_ref", "recoverability"],
        "properties": {
          "event_ref": {"type": "string"},
          "recoverability": {"enum": ["recoverable", "irreversible", "unknown"]},
          "is_origin": {"type": "boolean"},
          "explains": {"type": "array", "items": {"type": "string"}}
        }
      }
    },
    "hypotheses": {
      "type": "array", "minItems": 1, "maxItems": 4,
      "items": {
        "type": "object",
        "required": ["label", "layer", "support", "oppose", "posterior"],
        "properties": {
          "label": {"type": "string"},
          "layer": {"enum": ["L0_env", "L1_host", "L2_harness", "L3_protocol", "L4_firmware", "L5_device", "L6_spec"]},
          "statement": {"type": "string"},
          "support": {"type": "array", "items": {"type": "string", "pattern": "^EV-[0-9a-f]+$"}},
          "oppose":  {"type": "array", "items": {"type": "string", "pattern": "^EV-[0-9a-f]+$"}},
          "posterior": {"type": "number", "minimum": 0, "maximum": 1}
        }
      }
    },
    "information_gaps": {"type": "array", "items": {"type": "string"}},
    "reproduction": {"type": ["object", "null"]}
  }
}
```

**工程化校验**（在模型输出之后、写入 DB 之前）：
- `support`/`oppose` 中每个 `evidence_id` 必须存在于本次证据集 → 不存在则**删除该条引用**并把置信度打回 0.5 以下（防幻觉）。
- 顶层 `causal_chain[0]` 必须 `is_origin=true` 且 `recoverability=irreversible` → 否则整条结论降级。

### 7.3 环境预检基线（Pre-flight Baseline）

在分析前先做**环境健康快照**，作为 M2 的辅助证据：

```
对每台执行主机持续采集基线：
  - PCIe AER correctable/uncorrectable 计数（每 10 分钟增量）
  - 链路速度/宽度/协商状态抖动次数
  - NVMe SMART: media_errors, unsafe_shutdowns, percentage_used, temperature, thermal_throttle_cnt
  - 主机温度、CPU 频率降频标记
  - 供电事件（IPMI/BMC 日志）
  - 共享端口占用（同机同 HBA 同时间其它 DUT）
```
异常主机（基线越界）被标记，失败落在异常主机上时，M2 判定自动加权为环境侧。

---

## 8. 前端模块设计（嵌入现有测试平台）

### 8.1 集成方式

- **推荐**：作为平台内独立路由/微前端模块（iframe 或 module federation），复用平台的登录态与权限体系；平台仅需在失败详情页加一个"AI 分析"Tab。
- **备选**：独立前端应用 + 平台内链接跳转（集成成本最低，能力受限）。
- **回传通道（与供料方向对称）**：结论、证据链、处置建议由 §7.1 接口返回，**由平台前端调用**并渲染在「AI 分析」Tab 内；**平台数据库与用例库不被本系统写入**，平台侧只需新增一个前端路由与嵌入点。若后续要把处置动作（隔离、复跑、拦截）反向回灌给本系统，则需平台额外开放写接口——V0 不需要。

### 8.2 页面清单

| 页面 | 关键内容 |
|---|---|
| **失败详情 - AI 分析 Tab** | 结论卡片（责任域/置信度/是否已确证/处置建议）+ 证据链时间线 + 候选假设对比 + 复现状态 + 反馈按钮 + 相似历史 |
| **证据链时间线** | 按时间轴展示事件；可逆/不可逆着色；不可逆边界高亮标注"疑似因果起点"；点击展开原始日志行 |
| **同批次对照面板** | 主机 × 被测件失败热力图；本例位置高亮；结论的依据统计量（Fisher p、收缩失败率、反事实对照说明） |
| **失效模式卡库** | 卡片列表 + 详情（证据模板/规则/配方/回归用例/处置手册/血缘/置信度历史） |
| **复现实验室** | 配方列表、提交复现、执行日志、签名匹配结果 |
| **经验运营看板** | 首页四块：经验沉淀量 · 命中情况 · 命中分布 · 漏网与误判（§2.6）；下钻展开：准确率、误报率、采纳率、平均定位时长、逃逸分析、噪声分布、规则集版本与命中统计、策略择优收敛曲线、误判护栏清单、回归护栏指标、灰度与回滚操作 |
| **我的待办** | 待确认队列（P3/低置信/竞争假设） |

### 8.3 交互要点

- **渐进披露**：默认显示结论与三行依据；细节（完整证据链、原始日志）在展开/跳转后才加载（避免"日志墙"）。
- **对比式呈现**：多个候选假设并排显示支持/反对证据，由人一眼看出差异。
- **一键确证**：对已有配方的失败，提供"立即复现"按钮。
- **解释性**：每个数字都可下钻（置信度 → 校准曲线点；失败率 → 原始样本）。

---

## 9. 部署与技术选型

### 9.1 部署拓扑（内网 Linux）

```
                    ┌──────────── 内网 DMZ ────────────┐
                    │  钉钉网关（出网，仅此一处白名单）    │
                    │  Qoder Cloud（若涉敏等级允许）      │
                    └───────────────────────────────────┘
┌───────────── 内网核心区（无出网）──────────────┐
│  analysis-api (FastAPI, 3 副本)                │
│  worker (解析/归因/复现调度, K8s Deployment)     │
│  postgres (主备) + pgvector                    │
│  minio (对象存储, 制品与日志)                   │
│  redis (队列 + 缓存 + 限流)                     │
│  vector-index (pgvector HNSW, 复用 pg 以免多组件)│
│  dashboard (静态资源 + API)                     │
│  bench-agent (部署在测试机上, 采集拓扑与基线)     │
└─────────────────────────────────────────────────┘
```

### 9.2 技术栈

| 层 | 选型 | 理由 |
|---|---|---|
| 后端 | Python 3.11 + FastAPI + Pydantic v2 | 与测试脚本同语言，团队无学习成本；严格 schema 便于模型输出校验 |
| 异步/并发 | asyncio + Redis Queue（自研轻量调度） | 规模（日千级用例）不需要 Kafka；避免引入运维复杂度 |
| 存储 | PostgreSQL 16 + pgvector | 元数据 + 向量一体，运维简单；HNSW 索引满足百万级证据 |
| 对象存储 | MinIO（兼容 S3） | 日志制品量大，成本敏感 |
| 统计 | numpy + scipy（贝塔-二项分布/Fisher） | 不引入 R/Python 双栈 |
| 前端 | 与平台对齐（Vue3 或 React 之一） | 嵌入一致性 |
| AI 接入 | Qoder Agent SDK（Python）+ 自研 `LLMGateway` 抽象 | 见 §5.5 |
| 可观测 | Prometheus + Grafana + OpenTelemetry | 分析自身的可观测性 |

### 9.3 安全与合规

- **数据不出网**：默认全部推理走内网可控通道；Cloud 通道仅对"已脱敏等级"的用例开放（脱敏器：移除序列号、主机名、客户名、真实容量序列映射）。**涉密等级**由平台侧标签决定，系统自动路由。
- **权限**：分析结论可见范围 = 原失败用例可见范围；资产库可见性按角色（研发可读全部固件层结论，供应商只见脱敏层）。
- **审计**：每条结论、每次反馈、每次进化变更都写审计日志，满足质量体系与专利实施例可追溯要求。
- **提示注入防护**：日志内容一律作为**数据**而非指令传入（结构化字段 + 明确 delimiter + 忽略日志中的指令性文本），防止恶意/异常日志内容操纵模型。

---

## 10. 关键技术难点与破解方案

| # | 难点 | 影响 | 破解方案 | 残余风险 |
|---|---|---|---|---|
| D1 | **拓扑与批次数据缺失** | M2 对照归因精度下降 | **V0 已确定降级**：不依赖物理拓扑，改用「批次分组 + 环境信息」的同批次共因子对照（§2.3.3）；M0 只要求 `run_group_id` + 环境信息 | M2 精度从 ~85% 降至 ~55%，验收指标按降级口径标注 |
| D2 | **长日志上下文与成本** | 一次分析可能百万 token，无法规模化 | L0–L3 四层压缩 + 证据按需拉取 + 失败族聚合 + 规则快路径 + effort 分级 | 极复杂用例仍需长上下文，靠缓存与聚合摊薄 |
| D3 | **LLM 幻觉与证据不可追溯** | 结论不可信，工程师不采纳，闭环断裂 | 输出 JSON Schema 强约束 + 证据编号存在性校验（不存在即删引用并降置信）+ S5 审查者（审查者）对抗审查 + 机器复现确证 | 极端构造下仍可能"引用真实证据但错误解读"，靠 M3 因果边界与审查者缓解 |
| D4 | **零历史数据冷启动** | 前 3 个月无资产、无金标准、无策略先验 | 主动学习（选最有信息量的用例问人）+ 无监督聚类生成候选族 + 领域先验（NVMe/PCIe 错误码字典、因果先验图）+ 种子知识（厂商 errata/release note 抓取）+ 复现确证作为快速确证通道 | 冷启动期准确率必然低，需向组织明确"前 8 周以收集标注为主" |
| D5 | **复现成本与资源争抢** | 复现排队长，确证延迟 | 分级配方（L0 快速/L1 标准/L2 全量）+ 影子机分时复用 + 成本上限 + 优先级队列 + 资源不足时标 inconclusive 而非证伪 | 高价值长稳问题仍需等待窗口 |
| D6 | **知识库污染与错误放大** | 一次错误采纳污染检索，后续全错 | 双闸门（离线金标准 + 影子）+ 灰度 + 秒级回滚 + 资产需确认来源准入 + M6 护栏 + 卡片版本血缘与 `deprecated` 状态 | 灰度期内存在双版本不一致，人工认知成本 |
| D7 | **反馈稀疏与噪声** | 多臂老虎机学不到东西 | 多信号（accept/correct/reject/ignore/响应时长）+ 奖励整形（correct=+0.2 弱正）+ 状态定义粗粒度（失败族级而非用例级）+ 先验强制探索 | 反馈率 <5% 时收敛慢，需产品设计保证低摩擦反馈 |
| D8 | **环境类误报难判** | 直接影响缺陷库可信度 | M2 对照归因 + 预检基线 + 因果图中显式建模 thermal/CRC/power 链 + 降级链中"先排除环境" | 环境与被测件交互型（如某盘在某背板才失败）仍是最难类别 |
| D9 | **通知误报疲劳** | 通知被屏蔽，系统等于不存在 | 指纹聚合 + 复发抑制 + 责任域自适应门限 + P3 走看板 + 通知预算 + 采纳率作为一级 KPI | 组织文化依赖，需要先建立"驳回也是有用反馈"的共识 |
| D10 | **Qoder SDK experimental 风险** | SDK 变更导致生产链路故障 | `LLMGateway` 抽象 + 只依赖稳定契约（prompt/JSON/工具协议）+ 不依赖 SDK 内部字段 + 本地 CLI 降级路径 + 契约测试 | Cloud 特有能力（session 持久化）不可用时需自建状态存储 |
| D11 | **并发多盘互扰** | 假失败、真失败混杂 | bench_sharing_mode 纳入环境向量；共享端口进对照网格；预检脚本建议独占 | 彻底解决需物理隔离，成本高，仅作长期建议 |
| D12 | **规格符合性判定** | 误把"符合规格的极限"报为缺陷 | 因果图 thermal→latency→timeout 边 + 对照 spec 限值 + 温度/功耗历史基线 + "极限表现"独立标签 | 依赖 spec 数据可结构化，缺失时只能靠人工 |

---

## 11. 专利布局

### 11.1 布局策略

**不主张**："使用 AI 分析日志"（现有技术密集，易驳回）。
**主张**：具备可量化技术效果的具体技术手段组合。

建议拆分为 **3 件发明专利**（可同族或分案），形成"机制—效果"链条：

| 编号 | 主题 | 核心独立技术特征 | 预期效果表述 |
|---|---|---|---|
| **P1** | 存储设备测试失败的**双层指纹与环境敏感归一化**方法 | 将失败特征解耦为环境无关的症状签名与环境向量；复合键决定资产适用边界；环境兼容性打分控制结论迁移 | 跨机器跨盘号聚合一致率提升；避免环境错配导致的根因误判 |
| **P2** | 基于**同批次对照与不可逆性边界**的存储测试失败根因归因方法 | 构造被测件×环境的反事实对照网格并做统计显著性判定；按可恢复性划分因果级联并定位首个不可逆事件作为因果起点 | 环境误报识别率提升；根因层级正确率提升 |
| **P3** | **假设-复现-确证与三通道自进化**的测试失败分析系统 | 假设生成→结构化复现配方→影子真机执行→签名匹配确证→三通道（规则蒸馏/策略择优/测试资产反向生成）演进→双闸门灰度回滚→误判判别式挖掘 | 根因可信度提升；重复问题分析成本下降；逃逸率下降 |

可选 **P4**（工程保护）：测试失败分析的**故障注入与影子调度**方法（配方化复现的资源预算调度）。

### 11.2 权利要求撰写要点

- **独立权利要求**（方法 + 系统/装置 + 存储介质三种形式）：必须写入**具体的数据结构与判定步骤**，且步骤之间存在**数据依赖关系**（避免被认定为纯智力活动规则）。
  - 例：P2 的独立权利要求应包含：① 以被测件标识与环境标识为轴构造对照网格；② 计算收缩失败率并做显著性检验；③ 按可恢复性属性划分事件流并定位首个不可逆事件；④ 将两者结果融合以输出责任域。
- **从属权利要求**（10~15 项）：锁定贝塔-二项分布收缩公式、反事实对定义、因果图边、温度降级链、指纹字段组合、复现配方 YAML 结构等 —— 这些细节构成**防御纵深**，即使独权被驳，从权仍可存活。
- **避免落入"纯算法"**：所有步骤必须绑定存储介质、IO 通路、固件/驱动/测试框架层级、具体日志字段（NVMe status code、PCIe AER 计数器、SMART 计数器增量）。这也是必须写进**实施例**的具体内容。
- **实施例数据**：需准备 3~5 个真实失败案例的完整链路（脱敏后）：日志片段、对照网格统计、因果链、复现记录、进化前后指标对比。这是专利可信度的关键。

### 11.3 现有技术规避检索方向（提交前必做）

需在提交前针对以下方向做检索并撰写规避说明（**注意：以下为需排查的技术方向，不代表已确认的对比文件，需实际检索后据此调整权利要求**）：
- 日志根因分析的图/规则/因果推断方法
- AIOps 中的"日志模板聚类 + 知识库复用"
- 混沌工程中的故障复现与实验回放
- 存储设备健康评估与介质错误预测
- 同批次/对照组异常检测（Canary/变更关联分析方法）
- 反馈驱动的提示词自动优化与 LLM 自改进循环
- 主动学习与置信度校准在运维告警中的应用

#### 11.3.1 现有技术检索快照（2026-11-18，GitHub + 公开资料）

已完成的初步检索表明：**"用 AI 分析测试失败日志"这一层已被大量公开方案覆盖，不可作为新颖点主张**。以下为已确认存在的对比方向，权利要求必须从"泛化 AI 归因"下沉到具体技术手段。

**（一）AI 测试失败归因赛道已拥挤（均为 Web/云软件测试域）**

| 方案 | 形态 | 已公开的能力（对我方的限制） |
|---|---|---|
| BrowserStack Test Failure Analysis Agent | 商业 SaaS | 解析日志/执行历史/堆栈，把失败归类为 **产品 / 环境 / 测试脚本** 三类并给修复建议 |
| Sentry Seer | 商业 SaaS | 跨堆栈/提交/span 的 RCA，自称 **94.5% 诊断准确率**，可自动开 PR |
| Datadog Test Optimization | 商业 SaaS | CI 测试 span 与生产 APM/RUM trace 关联 |
| Sauce Labs Failure Analytics | 商业 SaaS | 历史套件失败模式分析 + 系统性 flaky 检测 + ML 失败分类 |
| ReportPortal | 开源 Apache-2.0 | ML Auto-Analyzer（XGBoost ~30 特征含 cosine 相似度 + 随机森林概率）、**正则签名规则去重已知错误**、**按项目重训模型** |
| Testkube AI | 开源 | 失败与 Git commit 关联，自动生成修复 PR |
| PyRCA（Salesforce） | 开源 BSD-3 | 因果发现 **PC / GES / LiNGAM**、Random Walk、贝叶斯推断、**ε-Diagnosis** 根因定位 |
| RCAEval | 开源 MIT | metric/trace/log 三类 RCA 算法的 **benchmark** 与可复现数据集 |
| TeamCity QA Intelligence | 开源 MCP | 19 个诊断工具，三分类：**代码变更 / 测试不稳定 / 基础设施故障** |
| Flaky(Box) / Captain(RWX) / CTRF | 开源 | flaky 重跑判定、隔离(quarantine)、失败摘要 |
| TestTriage / ai-cicd-failure-triage-agent / testops-copilot / Bug-Analysis-Dashboard / deep-trace / Direly / testsense-ai / ai-test-failure-analyzer | 开源小项目 | 分别为 RAG 失败分析、CI 失败分诊、Robot Framework + RCA、five-whys、**可复跑修复验证**等 |

> **结论 1**：**"环境 vs 用例 vs 产品缺陷"的三分归因已是商业现有技术**（BrowserStack），不得作为独权特征。
> **结论 2**：**"因果推断/因果图做根因定位"已有成熟开源实现**（PyRCA），必须把新颖性收敛到"硬件测试批次内的对照网格构造 + 具体统计检验"。

**（二）对 M1（双层指纹）的高风险近似**

- `shakoorshkh/pebble`（Rust）：**"fingerprints CI failures, remembers successful recoveries, and safely reuses them"** —— 指纹 + 记忆复用，与 M1+M5 组合最接近，**必须逐条比对权利要求**。
- `davccavalcante/behavioralai`：per-agent **learned fingerprints** + drift detection + attribution。
- `sgunes16/Frameval`：**behavioral fingerprint + failure mode classification + recovery analysis**。
- `showingdata/sql-circuit-breaker`：**按 SQL 指纹维度**的状态机与熔断（指纹作为聚合键的工程先例）。
- ReportPortal 的"已知错误签名正则去重"与 per-project retrained model（与 M5 规则蒸馏通道可类比）。

> **规避写法**：独权必须写入 ①**环境无关症状签名 × 环境向量**的**解耦**（而非单层指纹）、②**复合键**决定资产适用边界、③**环境兼容度打分**控制结论迁移——这三点是单层指纹方案不具备的。

**（三）对 M2/M4（反事实对照与复现确证）的高风险近似**

- `jaineet17/causal-agent-replay`（★16）：**"Counterfactual replay and causal attribution … find which step actually caused a failure by intervening on it"** —— 反事实重放 + 干预式归因，是 M2/M4 概念上最接近的公开实现（域为 LLM agent 轨迹）。
- `funkelab/quac`（Quantitative Attributions with Counterfactuals）、`PayamDiba/CIMLA`（Counterfactual Inference by ML and Attribution Models）、`kesjien/wavexplain`（多序列时序预测的反事实归因）。
- AgentFail 论文（arXiv 2509.23735）提出 **RLDetect**：Trajectory Analyst 定位可疑节点 + **re-rollout-based counterfactual verification**（固定某节点输出后整体是否成功 → 判定该节点为根因），并报告"提供根因信息可使自动修复成功率从 16.3% 提升到 66.8%"。

> **规避写法**：不得主张"用反事实重放找根因"这一抽象思想。必须绑定：①**DUT 标识 × 环境标识**两轴构成的**批次内 2×2 对照网格**（非轨迹节点干预）、②**Beta-Binomial 收缩 + Fisher 显著性**的具体检验流程、③**NVMe/PCIe 语义**下的可恢复性属性判定、④**责任域 L0–L6** 分层输出。

**（四）SSD/存储域：领域空白已确认**

| 项目 | 性质 | 说明 |
|---|---|---|
| `pynvme/pynvme`（★217） | 执行框架 | NVMe/PCIe/SPDK 测试工具，**不做失败归因** |
| `earlephilhower/ezfio`（★201） | 执行框架 | NVMe/SAS/SATA SSD 测试框架，**不做失败归因** |
| `007revad/Synology_SMART_info`（★152） | 工具 | SMART 属性展示 |
| `coltonshane/SSD_Test`（★25） | 测试工程 | Zynq Ultrascale+ 裸机 NVMe 测试 |
| `gtcomm/diskwipe` / `joelandman/disk_test_setup` | 工具 | 磁盘擦除/测试自动化 |

> **结论 3（最有利）**：**所有 AI 归因方案都在 Web/云软件测试域；SSD/NVMe 域只有执行框架，没有一个做失败 AI 归因**。因此"**存储设备测试域 + 硬件拓扑（主机↔HBA↔背板↔温区）+ 责任域 L0–L6 + NVMe status code/PCIe AER 语义 + 真实影子真机复现**"这一整套限定，是本方案最强的护城河，也构成独权中的必要技术特征。

**（五）尚未检索、提交前必须补做的方向**

- 中文专利库（CNIPA / 佰腾 / 智慧芽）："测试用例 失败 归因 指纹 聚类""反事实 对照组 批次 硬件 测试""失效模式 卡 知识库 进化""SSD 失效 复现 配方"
- IEEE/ACM 会议：故障级联最早事件定位（earliest failure localization）、delta debugging 在硬件测试中的最小复现
- 厂商白皮书/专利：Samsung / SK Hynix / Micron / Intel 的 SSD 测试与失效分析方法；Delphi / Cadence / Synopsys 的存储 IP 验证方法
- 中国专利：测试系统自身的故障注入与影子调度（对应可选 P4）

### 11.4 申报节奏

| 时点 | 动作 |
|---|---|
| M0 结束 | 完成**发明交底书**（内部）：P1/P2/P3 各一份，含机制描述、流程图、可量化数据占位 |
| M2 中期 | 补齐 P1 实施例数据（指纹聚合一致性对比实验），提交 **P1** |
| M3 结束 | 补齐 P2 实施例数据（对照归因 A/B 对比），提交 **P2** |
| M4 结束 | 补齐 P3 实施例数据（三通道演进前后对比、误判收敛曲线），提交 **P3** |
| M5 | 视反馈补充从属权利要求与分案；同步申报**软件著作权**（架构与代码资产保护） |

**配套的知识产权布局**：
- 失效模式卡库、复现配方库、因果先验图、规则库 → 作为**商业秘密**管理（不进专利全文，放实施例中概述），避免专利公开削弱壁垒。
- 平台侧新增模块与 `bench_topology` 表结构 → 软著 + 商业秘密。

---

## 12. 演进路线图

> 每阶段都有**可验收的量化出口**，避免"做完一半无法判断是否值得继续"。

### M0 · 打通与地基（4~6 周）

**目标**：能自动把失败事件接进来，出结论并通知到人；建立后续一切工作的地基。

- 平台侧改造（**关键路径**）：暴露 `run_group_id`（批次分组）、失败用例日志制品访问方式、失败事件推送 API、环境信息接口；**前端**：失败详情页增加「AI 分析」Tab 与内嵌模块路由（§8.1），用于回传结论的展示。**不要求 `bench_topology`**（V0 降级为批次共因子对照，见 §2.3.3）。
- 采集器 `collector`：拉取失败事件 + 收集失败用例日志制品 + 环境/批次信息。
- 解析器 v1：用例 log、dmesg、nvme-cli smart/error log、Python traceback、 fio 输出（这五类覆盖 ≥85% 场景）。
- **代码事实抽取 v1（两段式）**：① Python 脚本静态解析（用例清单、参数矩阵、断言位置、依赖有向无环图、日志→代码行锚点）→ ② AI 二次核实（断言语义、失败意图、依赖边复核），**核实通过才入库**，产出代码事实图谱；按 `script_path + script_version` 缓存，脚本未变不重解析，核实只针对本次引用到的用例与断言。
- **漏斗 v1**：一级初筛（本地规则 + 大面积环境异常拦截）+ **免 AI 截流旁路** + 二级细筛（证据体检）+ 指纹去重 + **级联抑制**（保留链路首个失败点）。三级精筛（策略路由）V0 先用**固定策略表**，随 M3 的策略择优一并转为在线学习。
- **基线与变更对比**：先只做"已知"匹配，冷启动期基线不足不做新增/回归判定。
- 指纹 v1：`symptom_signature`（错误码 + 模板序列 + 阶段 + 断言类型）。
- 分析 v1：单事件归因（LLM 直接读 L2/L3 摘要 + 少量样本），输出 Schema 约束结论。
- 通知 v1：钉钉机器人卡片 + 工作通知 + 反馈回调（采纳/纠正/驳回三个动作）。
- 前端 v1：失败详情 AI 分析 Tab（结论 + 证据 + 反馈按钮）。

**出口标准**：
- 事件接入成功率 ≥ 99%，分析覆盖率 ≥ 90%。
- 一级初筛规则命中占比 ≥ 20%（免 AI 比例），漏斗整体压缩比 ≥ 10:1。
- 单例分析 P95 ≤ 60s，成本 ≤ ¥0.8/例。
- 人工抽检 100 例：责任域判定准确率 ≥ 65%（冷启动基线）。
- 反馈可用性：至少 100 条人工反馈被采集。

### M1 · 归因内核（6~8 周）

- **M2 同批次对照归因**落地（统计模块 + 契约测试）。
- **M3 不可逆性边界**检测 + SSD 因果先验图 v1。
- 多源时间对齐（串口/BMC 时间标记注入）。
- 置信度校准（用 M0 采集的反馈做 Platt 校准）。
- 指纹 v2：加入 `environment_vector` 与 `composite_key`，环境兼容度打分。
- 前端：证据链时间线 + 同批次对照热力图。

**出口标准**：
- 责任域判定准确率 ≥ 78%。
- 环境误报识别率（抽样 200 例）≥ 80%。
- 归因结论可区分被测件/环境/框架/随机四类，准确率 ≥ 75%。

### M2 · 资产与复用（6~8 周）

- 失效模式卡库 + 失效族聚合视图 + 相似案例检索（向量 + 指纹双路）。
- 复现配方生成（LLM 生成 YAML）+ 影子调度器 v1（占用预算控制）。
- 检测规则蒸馏通道 v1（含离线金标准集与影子评估框架）。
- 置信度-通知门限联动。

**出口标准**：
- 命中失效模式卡的复用率 ≥ 25%（同类问题直接命中）。
- 复现配方自动生成可用率 ≥ 70%，人工修正后执行成功率 ≥ 60%。
- 规则快路径覆盖 ≥ 15%，命中部分零 LLM 成本。

### M3 · 复现确证与策略进化（8~10 周）

- 复现闭环完整跑通（含签名匹配、refuted/inconclusive 区分）。
- 策略择优（汤普森采样）上线，含先验强制探索。
- 离线评估框架（300+ 条金标准）自动化，接入每次变更的 CI 闸门。
- 灰度与一键回滚。

**出口标准**：
- 已确证（machine_reproduction）资产占比 ≥ 40%。
- 策略择优相对固定策略在金标准集上准确率提升 ≥ 5 个百分点。
- 回滚演练通过（变更 → 评估 → 灰度 → 回滚 ≤ 5 分钟）。

### M4 · 闭环进化与规模化（8~10 周）

- 测试资产反向生成通道（回归用例 PR、预检脚本、判据增强、用例隔离建议）——**V0 不落地，架构图上无出口**，本阶段整体补齐。
- 误判判别式挖掘（M6）+ 护栏回放验证。
- 逃逸分析（把线上缺陷与历史失败指纹关联，量化"曾出现过"的比例）。
- 质量看板完善 + 组织级度量（SLO 化）。

**出口标准**：
- 回归用例自动生成并合入 ≥ 10 条，覆盖 ≥ 5 个失效模式。
- `repeat_mistake_rate` 逐月下降 ≥ 30%。
- 逃逸缺陷中"历史曾出现过"占比 ≥ 50%。
- 平均定位时长相比基线下降 ≥ 70%。

### M5 · 专利与持续优化（持续）

- 专利提交与实施例数据补齐；商业秘密资产管理；SLO 化运营；扩展到其它存储形态（HDD/RAID 卡）与其它团队。

---

## 13. 度量体系

### 13.1 北极星指标

> **"从失败发生到根因确证（或被正确排除）的中位耗时"**，且**误报率**同时受控。

单一指标容易作弊（快速给个错结论），必须配对约束。

### 13.2 指标看板

| 类别 | 指标 | 目标 |
|---|---|---|
| **效率** | 端到端分析时延 P50/P95 | ≤ 30s / ≤ 3min |
| | 平均定位耗时（MTTR-定位） | 相比基线 ↓ 70% |
| | 单例分析成本 | ≤ ¥0.5（复现确证另计，M3 起） |
| **准确** | 责任域判定准确率 | ≥ 90%（M4） |
| | 根因 Top-1 准确率 | ≥ 85%（V0 以人工确认样本计，M3 后含复现确证样本） |
| | 误报率（被驳回/被人工判为无效的比例） | ≤ 8% |
| | 环境类误报识别率 | ≥ 85% |
| | 竞争假设误判率（top1/top2 差 <0.08 却错） | ≤ 5% |
| **闭环** | 人工反馈率 | ≥ 35%（P0/P1） |
| | 采纳率 / 纠正率 / 驳回率 | 采纳 ≥70%，驳回 ≤10% |
| | 复现配方生成→执行→确证成功率 | ≥ 50%（M3 起） |
| | 失效模式卡复用率 | ≥ 40%（M4） |
| **进化** | 规则快路径覆盖率 | ≥ 35% |
| | 多臂老虎机相对基线准确率增益 | ≥ 8 个百分点 |
| | `repeat_mistake_rate` 月环比 | ↓ ≥ 30% |
| | 逃逸缺陷历史曾出现率 | ≥ 50% |
| **通知** | 通知误报率 | ≤ 5% |
| | 通知→响应中位时长 | ≤ 30 min（P1） |
| | 每人日均通知条数 | ≤ 8 |

> **与经验运营看板的对应关系**：§2.6 的四块视图不是另起一套指标，而是本表的下钻入口——「经验沉淀量」对应闭环类与进化类指标的存量口径；「命中情况」对应规则快路径覆盖率与失效模式卡复用率；「命中分布」对应逃逸缺陷历史曾出现率的分组视图；「漏网与误判」对应误报率、采纳 / 驳回率与待确认积压。

### 13.3 组织机制

- **每周**：失败模式评审会（P0/P1 结论确认 + 误判复盘 + 规则/护栏新增）。
- **双周**：进化版本评审（策略择优收敛情况、新规则上线）。
- **月度**：逃逸分析会（把逃逸缺陷关联到指纹库，产出"本可拦截"清单 → 反哺预检与回归用例）。
- **知识回流**：确认结论 → 研发侧修复公告 → 回写失效模式卡 `disposition_playbook`。

---

## 14. 风险与应对

| 风险 | 等级 | 应对 |
|---|---|---|
| 平台侧拓扑/批次改造不被排期 | **高**（直接决定 M2 价值） | M0 即立项；准备降级方案（仅主机级/被测件级两档），并在立项时明确收益量化；把改造需求写成平台侧收益（误报减少→研发被打断次数下降） |
| 反馈率过低，闭环无法启动 | 高 | 反馈入口零成本（单键采纳）；钉钉卡片内直接操作；采纳率纳入团队 KPI；未采纳的通知自动降级而非重复推送 |
| LLM 成本超预算 | 中 | 分层压缩 + 规则快路径 + 聚合分析 + effort 分级 + 语义缓存；按失败指纹设月度预算阈值，超限自动降级到低 effort |
| 结论被工程师不信任（信任危机） | 中 | 每条结论必带证据与统计依据；提供"一键跳到原始日志"；机器复现确证标签；先在只读看板灰度 2 周再推送 |
| 复现占用真机影响业务测试 | 中 | 分时复用 + 预算上限 + 优先级队列 + 复现标记实验盘；与测试排期协商固定窗口 |
| 供应商/客户信息泄露 | 中 | 分级脱敏 + 数据不出网默认 + 权限与原用例一致 + 审计 |
| 提示注入（异常日志内容） | 中 | 日志作为数据传入 + delimiter + 结构化字段优先 + 忽略指令性文本；模型侧 system prompt 显式声明 |
| 关键人员（资深工程师）流失带走经验 | 中 | 经验外化为失效模式卡与因果图资产；每次采纳即资产化 |
| 专利申请被驳回或被规避 | 中 | 分 3 件布局 + 从权防御纵深 + 商业秘密兜底 + 实施例数据充分 |
| 系统被当成"甩锅工具"（测试 vs 研发的信任问题） | 中 | 定位是"减少无效打扰"，不是"判定谁错"；环境类结论归基础设施而非研发；沟通口径先对齐 |

---

## 15. 立即可执行的四件事（本周内）

1. **落平台改造需求**：`bench_topology` 表 + `run_group_id` + 失败事件推送 API（这是 M0 的关键路径，越早越好）。
2. **建第一版因果先验图与错误码字典**：把团队现有的 NVMe status code、PCIe AER 错误、SMART 字段、常见固件报错整理成结构化知识（这是冷启动期最重要的资产投入，也最能体现领域壁垒）。
3. **打通钉钉卡片闭环**：哪怕初版结论粗糙，也要让"失败 → 通知 → 一键采纳/驳回"这条链路先跑起来，因为**反馈数据是后续一切进化的燃料**，晚一周就少一周的进化曲线。
4. **统一命名与首个资产同批产出**：系统名 AITrace（智溯）、服务名 `aitrace`，PPT / 钉钉 / 专利交底书口径一致；同时用这套命名产出**第一张失效模式卡**与**第一条复现配方**，让命名与资产库一起长出来，而不是事后补标签。

---

## 附录 A · 术语表

| 术语 | 含义 |
|---|---|
| 被测件 | Device Under Test，被测件 |
| 基础设施 | 测试系统/环境侧（背板、线缆、HBA、供电、散热、主机 OS/driver） |
| UECC | Uncorrectable ECC，不可纠正 ECC 错误，介质类致命错误 |
| PLP | Power Loss Protection，掉电保护 |
| HMB | Host Memory Buffer，主机内存缓冲（DRAM-less 盘用） |
| AER | PCIe Advanced Error Reporting |
| 症状签名 | 环境无关的症状签名（双层指纹的上层） |
| 环境向量 | 环境向量（双层指纹的下层） |
| 不可逆性边界 | 因果级联中首个不可恢复事件，即因果起点 |
| 反事实对照 | 同批次中承担对照角色的执行（被测件侧或环境侧） |
| 失效模式卡 | FailureModeCard，系统沉淀的核心知识资产 |
| 复现配方 | 结构化、可执行的最小复现剧本 |
| 判别式 | 从误判中提炼的、可防止同类错误的判别规则 |
| 金标准集 | 人工标注、用于离线评估的基准样本集 |
| AITrace | 本系统名：AI 智能归因 + Trace 因果追踪；中文「智溯」 |
| 代码事实图谱 | 由测试脚本解析产出的用例清单/参数矩阵/断言语义/依赖有向无环图，跨版本复用 |
| 代码锚点 | 把日志中的失败点映射回具体代码行与断言的定位信息 |
| 级联抑制 | 漏斗第四级：只保留失败链路上的首个失败点，其余判为 consequence |
| 免 AI 截流 | 一级初筛判定环境类异常后绕过 AI 直接通知的旁路 |
| 基线对比 | 与最近基线比较判定新增/回归/已知，并关联最近的固件/脚本变更 |

## 附录 B · 责任域分层

| 层级 | 名称 | 典型根因 | 处置方 |
|---|---|---|---|
| L0 | 环境/物理层 | 背板、线缆、供电、温区、EMI、共享端口冲突 | 基础设施 |
| L1 | 主机/驱动层 | OS、kernel、nvme driver、blk 层、IOMMU、RAID 卡 | 平台/IT |
| L2 | 测试框架层 | 脚本 bug、断言错误、参数越界、并发冲突、超时设置 | 测试开发 |
| L3 | 协议/链路层 | PCIe 链路、NVMe 协议合规性、链路训练异常 | 硬件/固件 |
| L4 | 固件层 | FTL、GC、磨损均衡、队列管理、HMB、PLP、固件版本 bug | 固件研发 |
| L5 | 器件层 | NAND die、DRAM、控制器 die、焊点、老化 | 硬件/供应链 |
| L6 | 规格/设计层 | 规格符合但裕量不足、设计边界 | 产品/系统架构 |
