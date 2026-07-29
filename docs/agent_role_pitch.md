# 面向“大厂 Agent / LLM 应用工程岗位”的项目包装与面试准备

本文是 [`resume_project.md`](resume_project.md) 的 Agent 岗位视角版本：同一套数字与证据，按“规划—执行—重规划工作流、工具编排、可观测、可评测”的叙事重新组织。所有数字的唯一可信来源仍是 `resume_project.md`，本文不新增任何数字；唯一的例外是 2026-07-27 可靠性升级的工程参数（超时、TTL 驱逐、409 去重等），以 `app/agent/mcp_client.py`、`app/sse.py`、`app/api/chat.py` 代码为准。

## 项目一句话定位

> 一个可运行、可复现、可评测的 Agent 工程作品：以 LangGraph `Plan → Execute → Replan` 诊断工作流和 ReAct 对话 Agent 为主线，通过 FastMCP 统一编排 19 个工具，配套双模型路由、SSE 流式与断线重放、RAG 检索质量实验和端到端 Agent 评测，把“调 LLM API”做成了有工作流收敛、有工具治理、有量化评测的完整 Agent 系统。

对 Agent 岗位，叙事重点是 **Agent 基础设施能力**（状态图、工具协议、评测、可靠性），运维业务只是载体。不要说“我做了一个运维机器人”，要说“我做了一个可评测的多工具 Agent 平台，运维诊断是它的验证场景”。

## 简历项目描述

### 一句话版

> **AIOps Copilot**：多工具诊断 Agent 平台：以 LangGraph Plan–Execute–Replan 状态图编排诊断工作流，通过 FastMCP 统一治理 19 个工具（超时、分类重试、熔断、只读边界），SSE 流式支持断线重放与幂等去重，并以 600 条防泄漏数据集、54 组对照实验和端到端任务评测建立可复现的量化证据。

### 要点版（5 条，可直接粘贴）

> **AIOps Copilot｜多工具诊断 Agent 平台｜核心开发｜2025.10–2026.03｜LangGraph / MCP / RAG / SSE / FastAPI / Prometheus**
>
> - 基于 LangGraph 设计 Plan–Execute–Replan 诊断工作流（Planner/Executor/Replanner 状态图 + checkpoint 断点恢复），与 ReAct 对话 Agent 按问题复杂度分流；工作流设置最大步骤数与强制收敛条件，端到端 P50/P95 为 9.36/17.36 秒。
> - 通过 FastMCP 统一编排 19 个工具（17 个 MCP + 2 个本地，3 个 MCP Server）；自研客户端拦截器实现单次执行超时（30s）、错误分类重试（熔断/舱壁/参数错误不重试）、熔断器与舱壁隔离；MySQL 只读边界（单条 SELECT、限行 200、schema 白名单）。
> - SSE 事件带单调递增 ID，支持 Last-Event-ID 断线重放与 X-Idempotency-Key 幂等；replay buffer 有界并在 terminal 事件后按 TTL 驱逐；同一操作并发重连返回 409 去重，避免弱网重连重复执行整个 Agent。
> - 建立防泄漏评测方法论：600 条查询按问题模板隔离 train/dev/test=360/120/120，dev 上完成 54 组对照实验锁定配置，在 120 条 held-out test 上将 BGE Hit@5 从 95.83% 提升至 98.33%、MRR@5 从 68.79% 提升至 76.06%；30 条 Agent 任务 × 3 轮，任务成功率 80%、工具调用成功率 100%。
> - 可靠性治理：入口有界并发 + 429 准入控制（排队 1s 超时返回 Retry-After）、API Key RBAC 三级（viewer/operator/admin）、Prometheus 指标 + SLO 错误预算 + OpenTelemetry 追踪；122 个自动化测试通过，语句覆盖率 72.16%。

版面紧张时保留第 1、3、4 条：工作流编排、流式工程、评测方法论——这三条与 WindOS 项目的差异最大。

> 注：第 2、3 条中的单次执行超时、错误分类重试（熔断/舱壁不参与重试）、terminal 后 TTL 驱逐、并发重连 409 去重为 2026-07-27 升级后实现，代码见 `app/agent/mcp_client.py`、`app/sse.py`、`app/api/chat.py`。

## 与 WindOS 项目的差异化对照

简历上两个项目都出现"LLM + RAG + 工具调用"，必须让评审一眼看出它们证明的是两种能力：WindOS 证明**垂直业务建模与混合架构**，AIOps Copilot 证明 **Agent 平台工程与可靠性治理**。

| 维度 | WindOS（海上风电智能排程） | AIOps Copilot（多工具诊断 Agent 平台） |
|---|---|---|
| 证明什么 | 垂直业务建模、LLM 与确定性求解器协作 | Agent 平台工程、工具治理、可靠性治理 |
| 核心难点 | 气象/船舶/人员多资源硬约束、排程可行性 | 多源工具统一治理、流式协议、可复现评测 |
| Agent 主线 | 自研多轮对话 Agent 框架 + ReAct 分步决策 | LangGraph Plan–Execute–Replan 状态图 + checkpoint，按复杂度分流 |
| 架构特征 | "LLM 意图理解 + OR-Tools 确定性排程"混合架构 | MCP 统一工具协议 + 拦截器治理（超时/分类重试/熔断/舱壁） |
| 检索口径 | Milvus 5,247 条知识索引，Hit@5 92% | 600 条防泄漏数据集、54 组 dev 实验、held-out test Hit@5 98.33% |
| 流式工程 | 6 类 SSE 业务事件实时返回 | 事件 ID + Last-Event-ID 重放 + 幂等键 + 有界 replay buffer + 409 去重 |
| 可靠性 | 不作为主线 | 429 准入控制、RBAC 三级、SLO 错误预算、122 测试 / 72.16% 覆盖率 |
| 面试标签 | "懂业务约束和算法落地" | "懂 Agent 基础设施、流式工程和评测方法论" |

讲述原则：ReAct 分步决策、多轮 Memory/指代消解、Milvus 索引构建这些角度留给 WindOS；AIOps Copilot 的第一句从"工具治理与流式协议"讲起，两个项目的第一句不重复技术名词。

面试官说"两个项目看起来重叠"时的一分钟答法：

> "两个项目都用了 LLM 和向量检索，但证明的是两种能力。WindOS 是垂直业务项目，难点在把气象、船舶、人员这些硬约束建模成优化问题，用'LLM 意图理解 + OR-Tools 确定性求解'的混合架构落地，回答的是'LLM 怎么和算法系统协作'。AIOps Copilot 是平台工程项目，难点在工程底座：19 个 MCP 工具的超时、分类重试、熔断舱壁，SSE 的断线重放、幂等和并发去重，600 条按模板防泄漏切分的数据集和 54 组对照实验，回答的是'怎么把 Agent 当成可靠分布式系统来运营'。一个是业务深度，一个是工程底座；技术上唯一的交集是都用了向量检索，但 WindOS 讲索引规模，这个项目讲防泄漏的评测方法论。"

## 技术亮点叙事（每条对齐仓库证据）

### 1. LangGraph Plan–Execute–Replan 诊断工作流与 checkpoint

- `app/agent/aiops/` 拆为 `planner.py`、`executor.py`、`replanner.py`、`state.py`：Planner 生成步骤计划，Executor 逐步执行并把结果写回状态，Replanner 根据证据决定继续、重规划或收敛输出。
- 使用 LangGraph checkpointer 持久化会话状态，默认 `MemorySaver`，可切换 PostgreSQL checkpoint（`app/services/aiops_service.py`、`app/services/rag_agent_service.py` 的 `configure_checkpointer`）；thread_id 不复用调用方 session_id，避免跨请求 checkpoint 污染（`aiops_service.py:87` 注释）。
- 测试覆盖 Replanner 与 PostgreSQL checkpoint（见 `resume_project.md` 工程验证一节）。

### 2. ReAct 对话 Agent

- `app/services/rag_agent_service.py`：短链路知识问答走 ReAct 风格工具调用循环，响应路径短；SystemMessage 每轮动态拼入而不写入 checkpoint，避免同一 thread 重复累积。
- 与诊断工作流分流的标准是问题复杂度：开放问答走 ReAct，多证据排障走 Plan–Execute–Replan。

### 3. FastMCP 统一编排 19 个工具

- 3 个 MCP Server（CLS 日志 8003、Monitor 监控 8004、Ops 8005）实际注册 17 个 MCP 工具，加知识检索和时间共 19 个；Agent 只依赖统一工具协议，新增数据源只注册工具和 schema，不改主流程。
- `app/agent/mcp_client.py` 客户端拦截器统一实现错误分类、最多 3 次指数退避与随机抖动、调用成功率和 P50/P95 统计；`PermissionError`/`ValueError`/`TypeError` 等调用方错误不重试。

### 4. DeepSeek 双模型路由

- 工具规划与执行使用 V4 Flash 非思考模式，最终诊断报告使用 V4 Pro 思考模式；配置集中在 LLMFactory，健康接口不暴露密钥（README「核心能力」）。
- 收益逻辑：工具链是高频、结构化输出调用，用快模型控制时延和成本；最终报告是低频、需要推理的输出，用思考模式换质量。

### 5. SSE 流式输出 + Last-Event-ID 断线重放 + 幂等

- `app/api/chat.py:114` 与 `app/api/aiops.py:138`：SSE 事件带事件 ID 和游标，客户端断线后通过 `Last-Event-ID` 头请求重放，服务端按幂等语义去重，支持取消传播。
- 测试覆盖 SSE 游标与幂等回放、请求取消传播（`resume_project.md` 工程验证一节）。

### 6. RAG：本地 BGE + Milvus 与检索质量数字

- BGE Small 中文模型本地 Embedding（512 维）+ Milvus；运行时配置 plain 1200/0、Top-5，并从 Top-20 候选按来源文档去重，避免单一长文档占满召回位。
- 主口径（5 文档语料、120 条 held-out test）：初始 BGE 95.83%/68.79% → 优化 BGE 98.33%/76.06%（Hit@5/MRR@5）；BM25 对照 89.17%/57.75%。
- 扩展语料（10 文档）回归口径：70.00%/49.90% → 90.00%/59.40%，只能称为“扩展语料上的规则标签回归结果”。

### 7. 评测体系

- RAG：600 条规则构造查询（5 类场景各 120 条），按问题模板分组切分 train/dev/test=360/120/120，同一模板的 6 种改写只出现在一个集合；dev 上 54 组真实 BGE 实验锁定配置后才跑 test。
- Agent：30 条端到端任务 × 3 轮共 90 次请求，任务成功率 80.00%、工具调用成功率 100.00%、平均/P50/P95 时延 10.17/9.36/17.36 秒；另新增 12 条确定性故障场景（成功/超时/错误返回各覆盖日志、监控、MySQL、web_search），报告按工具输出成功率与失败归因。
- 失败案例通过 `evaluation/incident_review.py` 导出人工复核，报告显式区分 synthetic fixture 与真实故障。

### 8. 可靠性治理

- Agent 入口有界信号量 + 队列超时返回 429 和 `Retry-After`，防止慢模型拖垮工作线程；熔断/半开探测/隔离舱有测试覆盖。
- API Key RBAC：viewer 只读指标和状态，operator 发起诊断，admin 上传和重建索引；认证会话 ID 加密钥指纹前缀。
- MySQL 只读边界：应用层只接受 SELECT/SHOW/DESCRIBE/EXPLAIN，拒绝多语句、注释和写入关键字，限制 schema 和最大返回行数；数据库侧仍必须使用只读账号（第二道防线，不替代数据库权限）。

## 面试追问 Q&A

### Q1：为什么用 Plan–Execute–Replan 而不是纯 ReAct？

ReAct 是边想边做，每一步的工具选择都依赖上一步输出，链路一长容易漂移且没有全局视角；多证据排障需要先列出完整计划（查日志、查监控、查历史事件、查知识库），再按步骤采集证据，每步结果写回状态后由 Replanner 判断证据是否足够——不够就重规划，够了就收敛。代价是 LLM 调用次数更多，所以设置了最大步骤数和强制收敛条件。项目里两条链路都保留了：开放问答走 ReAct，复杂告警走状态图，按问题复杂度分流。

### Q2：评测数据集如何防泄漏？

600 条查询由 5 个知识文档种子 × 20 个问题模板 × 6 种改写规则生成。切分不按条随机分，而是按问题模板分组：同一模板的全部 6 种改写只会落在 train/dev/test 中的一个集合，防止“测试集里出现训练集同模板的近义改写”造成的虚高。调参（54 组实验）只看 dev，锁定配置后才在 120 条 held-out test 上汇报一次最终结果。证据：`evaluation/generate_datasets.py`、`tests/test_evaluation.py`。

### Q3：SSE 断线重连/重放怎么实现？

服务端给每个 SSE 事件分配单调递增的事件 ID 并缓冲在游标窗口内；客户端断线重连时在请求头带上 `Last-Event-ID`，服务端从该 ID 之后重放缓冲事件。配合幂等键语义，客户端对已收到的事件去重，保证“至少一次投递 + 幂等消费”等价于不丢不重。取消传播也做了：客户端断开会把取消信号传到下游模型/工具调用。测试覆盖游标、幂等回放和取消传播。

### Q4：工具只读边界如何保证？

两道防线。应用层：SQL 工具只接受 SELECT/SHOW/DESCRIBE/EXPLAIN，拒绝多语句、SQL 注释和写入/管理关键字，并限制 schema 白名单和最大返回行数（`mcp_servers/ops_server.py`、`tests/test_ops_server.py`）。数据库层：必须使用只读账号。面试时要主动说：应用层校验是第二道防线，不能替代数据库权限——只做应用层过滤是可以被构造输入绕过的思路之一，权限收敛才是根本。

### Q5：双模型路由的收益是什么？

按调用特征分流：工具规划与执行是高频、结构化输出、延迟敏感的调用，用 V4 Flash 非思考模式控制时延和成本；最终诊断报告是每请求一次、需要多证据综合推理的输出，用 V4 Pro 思考模式换质量。收益不是拍脑袋：端到端 P50 9.36 秒、P95 17.36 秒是在这条路由策略下实测的。配置集中在 LLMFactory，可以整体切换 Ollama 本地模型做成本对照。

### Q6：如何定位 Agent 失败案例？

三层。运行时：每次工具调用记录成功率、P50/P95 和错误分类，请求级有 `X-Request-ID` 串联 HTTP 与工具调用，可按请求定位慢点。离线评测：30 条任务 × 3 轮的报告按工具输出成功率和失败原因归因；新增 12 条确定性故障场景（每个工具的成功/超时/错误返回）专门验证故障处理路径。人工复核：`evaluation/incident_review.py` 导出失败案例供逐条复核，报告显式标记哪些是 synthetic fixture，不把合成故障说成真实生产故障率。

### Q7：MCP 的价值是什么？为什么不直接写 HTTP 客户端？

MCP 把工具发现、参数 schema 和调用协议统一了：Agent 启动时自动发现 3 个 Server 注册的 17 个工具及其 schema，不需要为日志、监控、数据库分别维护私有适配代码。新增一个数据源只是注册新工具，Agent 主流程零改动。重试、指标采集这些横切逻辑集中在客户端拦截器里做一遍，所有工具受益。

### Q8：429 准入控制解决什么问题？为什么不用普通限流？

瓶颈是模型调用和 MCP 请求的连接占用时长，不是 QPS。入口用有界信号量约束在途重任务，等待超过队列超时（默认 1 秒）就快速返回 429 和 `Retry-After`，让调用方退避，避免慢模型/慢下游触发请求无限堆积和级联故障。这是 admission control 而非 rate limit：控制的是并发占用，不是请求速率。当前是单进程状态，多副本部署需要配合网关限流和全局队列。

### Q9：Hit@5 提升 2.5 个百分点，幅度不大，怎么讲？

先讲口径：95.83% → 98.33% 是在 120 条 held-out test 上、参数在 dev 锁定后只跑一次的诚实结果，基线本身已经很高，提升空间本来就小。更有说服力的对照是扩展语料回归：10 文档语料上同一套优化把 Hit@5 从 70.00% 拉到 90.00%（+20 个百分点），说明切分策略和来源去重在更难的语料上收益更大。再讲方法论：BM25 对照（89.17%）、MRR 同步记录、bad case 归因（磁盘与延迟文档的排序混淆），体现的是完整实验流程而不是单个数字。

### Q10：工具重试如何避免放大故障？

最多 3 次，指数退避加随机抖动（防止多请求同步重试形成惊群）。关键是错误分类：`PermissionError`、`ValueError`、`TypeError` 等调用方错误立即失败不重试，只有传输/服务错误可重试；无论重试几次，最终只记录一次逻辑调用结果，避免指标被重试次数污染。另外综合诊断类场景对部分失败降级——并行请求独立证据源，单端点失败保留其余证据并显式标记缺失，比整体重试更重要。

### Q11：项目最大的不足是什么？

主动讲四点：知识库只有 10 份 Runbook，日志与监控是演示服务；600 条查询标签仍是 pending（规则生成、未经人工复核），只能叫“可复现离线查询集”；80% 成功率的 Agent 基线主要覆盖知识检索，新增 12 条故障场景还没形成同口径基线；指标、admission control 是单进程内存态，`/production/readiness` 会如实暴露这些阻断项。诚实闸门本身就是设计的一部分。

## 30 秒电梯陈述

> “我做了一个可评测的多工具 Agent 平台，用运维诊断做验证场景。核心是按复杂度分流的双工作流：开放问答走 ReAct，多证据排障走 LangGraph 的 Plan–Execute–Replan 状态图，带 checkpoint 断点恢复。工具层用 FastMCP 统一编排 19 个工具，模型按任务路由 Flash 和 Pro。我最花时间的不是调通，而是评测和可靠性：600 条按模板防泄漏切分的 RAG 数据集、30 条任务 3 轮的端到端评测、SSE 断线重放、429 准入控制和只读安全边界——每个能力都有量化数字和测试兜底。”

## 与“调 API 套壳项目”的差异化

| 维度 | 套壳 Demo | 本项目 |
|---|---|---|
| 工作流 | 单次 prompt + function calling 循环 | LangGraph 状态图，Plan/Execute/Replan 分工，最大步骤数与强制收敛 |
| 会话状态 | 内存 list 或没有 | LangGraph checkpointer，可切换 PostgreSQL，双实例交叉读写已验证 |
| 工具层 | 硬编码几个函数 | MCP 协议统一发现 19 个工具，错误分类重试、指标集中在拦截器 |
| 输出 | 一次性返回 | SSE 流式 + 事件 ID + Last-Event-ID 重放 + 幂等 + 取消传播 |
| 效果证明 | 截几张对话图 | 600 条防泄漏数据集、54 组 dev 实验、held-out test、30 条任务 × 3 轮、失败归因 |
| 可靠性 | 不考虑 | 429 准入控制、熔断/半开、RBAC、只读 SQL 双防线 |
| 质量门禁 | 没有测试 | 122 个测试、72.16% 语句覆盖率、Ruff、CI，数字自动生成到 `artifacts/project_evidence.json` |

一句话版本：

> “套壳项目证明‘我会调 API’，这个项目证明‘我会把 Agent 当成一个分布式系统来做’——有状态管理、有协议抽象、有背压、有评测、有质量门禁。”

## 诚实边界

### 有仓库证据、可以直接写的数字

- 122 个自动化测试全部通过，语句覆盖率 72.16%，Ruff 通过。
- 19 个工具（17 MCP + 2 本地），3 个 MCP Server 实际注册。
- RAG 主口径：120 条 held-out test，BGE Hit@5 95.83%→98.33%、MRR@5 68.79%→76.06%；BM25 对照 89.17%/57.75%；dev 上 54 组实验。
- RAG 扩展语料回归口径：70.00%/49.90%→90.00%/59.40%（仅限“扩展语料上的规则标签回归结果”表述）。
- Agent：30 条任务 × 3 轮，任务成功率 80.00%、工具调用成功率 100.00%、平均/P50/P95 时延 10.17/9.36/17.36 秒。
- 600 条查询按模板隔离 train/dev/test=360/120/120。

### 不能写或必须带限定语的说法

- **不能写**“人工标注评测集”：600 条标签 review_status 全是 pending，只能写“可复现离线查询集”或“规则构造评测集”。
- **不能写**“Hit@5 从 62% 提升”或“500+ 条内部标注数据”：无仓库证据（详见 `resume_project.md`）。
- **不能写**“接入生产日志/监控”：CLS 与 Monitor 是演示数据服务，只能写“封装日志与监控演示服务”或“完成可替换的 MCP 接口层”。
- **不能写**“响应时间从小时级缩短到分钟级”：人工计时数据为空，只能写实测系统 P50/P95。
- 新增 12 条故障场景属于 `synthetic_fault_fixture`，用于验证故障处理与报表归因，**不能**说成生产故障率或 SLA；其数字只有在保存完整运行报告后才能引用。
- 80% 成功率只来自旧的 30 条知识检索任务，不能与新增故障任务混为同一基线。
- 指标与 admission control 是单进程状态，跨进程熔断是下一阶段工作——被问到分布式部署时要主动说明。

## 证据索引

| 面试问题 | 文件 |
|---|---|
| 双工作流与 checkpoint | `app/services/rag_agent_service.py`、`app/services/aiops_service.py`、`app/agent/aiops/` |
| MCP 重试、错误分类与指标 | `app/agent/mcp_client.py` |
| SSE 事件 ID / Last-Event-ID 重放 | `app/api/chat.py`、`app/api/aiops.py` |
| MySQL 只读规则 | `mcp_servers/ops_server.py`、`tests/test_ops_server.py` |
| RAG 数据与防泄漏切分 | `evaluation/generate_datasets.py`、`tests/test_evaluation.py` |
| dev 参数实验 / held-out test | `evaluation/reports/rag_eval_local_20260715-222843.*`、`evaluation/reports/rag_baselines_20260715-222630.*` |
| Agent 端到端指标 | `evaluation/agent_eval.py`、`evaluation/reports/agent_eval_20260715-155944.*` |
| 失败案例人工复核 | `evaluation/incident_review.py`、`evaluation/label_review_server.py` |
