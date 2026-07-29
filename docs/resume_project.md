# AIOps Copilot 简历表述、证据与面试说明

## 一页简历推荐版本

如果这是个人项目，把“核心开发”改为“独立开发”；只有确实存在团队协作且你承担核心模块时，才保留“核心开发”。

**AIOps Copilot（智能运维 Agent）｜核心开发｜2025.10–2026.03**  
**技术栈：** Python / LangGraph / LangChain / DeepSeek V4 / RAG / MCP / Milvus / FastAPI

- 基于 LangGraph/LangChain 设计 ReAct 对话 Agent 与 Plan–Execute–Replan 诊断工作流，串联告警解析、知识检索、计划生成、工具执行、动态重规划与结构化建议输出；接入 DeepSeek V4，按任务路由 Flash 非思考模式与 Pro 思考模式，并通过 FastAPI/SSE 流式返回诊断过程。
- 基于 FastMCP 统一编排 19 个工具（17 个 MCP 工具 + 2 个本地工具），封装日志和 CPU/内存监控演示服务，接入只读 MySQL、Tavily 与 WINDOS 只读治理面；实现工具自动发现、错误分类、最多 3 次指数退避与抖动、部分失败降级，以及调用成功率和 P50/P95 时延统计。
- 构建 600 条可复现 RAG 查询，按问题模板隔离为 train/dev/test=360/120/120；在 dev 上完成切分策略、Chunk Size、Overlap、Top-K 共 54 组实验并锁定配置，在 120 条独立 test 查询上将 BGE Hit@5 从 95.83% 提升至 98.33%、MRR@5 从 68.79% 提升至 76.06%；另对 30 条 Agent 任务重复评测 3 次，任务成功率 80%、工具调用成功率 100%，端到端 P50/P95 为 9.36/17.36 秒。

如果版面只能容纳两条，保留第一条，并把第二、三条压缩为：

- 统一编排 19 个本地/MCP 工具，接入日志、监控、只读 MySQL、联网检索与 WINDOS 只读诊断，实现有界并发、分类重试、部分失败降级及 HTTP/工具成功率和 P50/P95 观测；构建按模板隔离的 600 条 RAG 查询与 30 条 Agent 任务评测，在独立 test 集上将 BGE Hit@5 从 95.83% 提升至 98.33%。

## 为什么不能继续使用原表述

原简历中的以下内容当前没有仓库证据，建议删除：

- “500+ 条内部标注数据”：当前 600 条查询由 5 个知识文档种子、20 个问题模板和 6 种改写规则生成，600 条的 review_status 都是 pending，不能称为“内部标注”或“人工标注”。
- “Hit@5 从 62% 提升至 85%+”：仓库中没有这组同口径报告。旧的全量集结果为 93.33%→97.83%，但全量数据同时参与调参和汇报；改进后的独立 test 结果为 95.83%→98.33%。
- “响应时间从小时级缩短至分钟级”：人工计时数据文件当前为空，不能把估算的人工排障耗时与 Agent 响应时间直接比较。现阶段只能写实测的系统 P50/P95。
- “接入生产日志/监控”：CLS 与 Monitor MCP 当前是演示数据服务。应表述为“封装日志与监控演示服务”或“完成可替换的 MCP 接口层”，不要暗示已接入真实生产系统。

## 数字口径与证据

### RAG

- 扩展语料（2026-07-23）：Runbook 从 5 份扩展到 10 份，新增数据库连接池、Kubernetes CrashLoop、消息队列积压、DNS/网络和 TLS 证书场景；运行时采用 BGE Small 中文向量、512 维、plain 1200/0、Top-5，并从 Top-20 候选中按来源文档去重，降低单一长文档占满召回位的风险。
- 扩展语料回归：固定 120 条 test 查询上，初始 BGE（markdown-header/800/100）Hit@5/MRR@5 为 70.00%/49.90%，优化 BGE（plain/1200/0 + 来源去重）为 90.00%/59.40%，Hit@5 提升 20.00 个百分点；报告为 `evaluation/reports/rag_baselines_20260723-134448.*`。
- 新口径边界：新增的 5 份文档尚未纳入查询标签生成；120 条 test 标签仍为 `pending`，因此 70%→90% 只能称为“扩展语料上的规则标签回归结果”，不能称为人工标注测试结果。它也不能反向证明论文中的 62% 起点。
- 数据：600 条规则构造查询，5 类场景各 120 条。
- 防泄漏切分：按问题模板分组，train/dev/test=360/120/120；同一模板的 6 种改写只会出现在一个集合。
- dev 调参：54 组真实 BGE 实验，覆盖 markdown-header/plain、Chunk Size 400/800/1200、Overlap 0/50/100、Top-K 1/3/5。
- dev 最优：plain、Chunk Size 1200、Overlap 0、Top-K 5，Hit@5 98.33%、MRR@5 82.32%。
- held-out test：初始 BGE 95.83%/68.79%，优化 BGE 98.33%/76.06%，Hit@5 提升 2.50 个百分点；BM25 为 89.17%/57.75%。
- 证据：evaluation/reports/rag_eval_local_20260715-222843.* 与 evaluation/reports/rag_baselines_20260715-222630.*。

这些标签仍未人工复核，因此可以写“可复现离线查询集”或“规则构造评测集”，不能写“人工标注评测集”。完成人工复核后，再把这一限制从简历表述中移除。

### Agent

- 任务：30 条内部知识问答任务，5 类场景，每类 6 条。
- 重复：3 次，共 90 次请求。
- 结果：任务成功率 80.00%，工具调用成功率 100.00%，平均/P50/P95 时延 10.17/9.36/17.36 秒。
- 新增覆盖：在原 30 条及其顺序不变的基础上，增加 12 条确定性故障场景，覆盖日志 `search_log`、监控 `query_cpu_metrics`、只读 MySQL `mysql_read_query` 和 `web_search`，每个工具各含成功、超时和错误返回任务；报告现在按工具输出调用成功率、端到端 P50/P95 与失败原因。
- 证据边界：上述 80.00%、100.00% 和 9.36/17.36 秒仍只来自旧的 30 条知识检索任务，不能与新增任务混为同一基线。新增 12 条的超时/错误标签属于 `synthetic_fault_fixture`，用于验证故障处理与报表归因，不代表生产后端的真实故障率、SLA 或恢复能力；只有保存新的完整运行报告后，才能引用新增覆盖的实测数字。
- 局限：新增任务只覆盖 4 个代表性外部工具，日志与监控仍是演示服务；它不能证明全部 19 个工具或真实生产依赖都完成了端到端可靠性评测。
- 证据：evaluation/reports/agent_eval_20260715-155944.*。

### 工程验证

- 当前 17 个 MCP 工具已由三个 Server 实际注册；加上知识检索和时间工具共 19 个。
- MySQL 演示库已实查出 incident_history、service_alerts、service_metrics 3 张表。
- 本地 BGE 与 Milvus 已完成 10 份 Runbook 索引。
- 当前自动化测试 122 个全部通过，Ruff 通过，整体语句覆盖率 72.16%；已覆盖 API/鉴权、SSE 游标与幂等回放、过载保护、熔断半开恢复、PostgreSQL checkpoint、Tracing、Replanner、真实 Milvus 容器契约、向量服务与请求取消传播。

## 30 秒项目介绍

“这个项目解决的是告警发生后信息分散、排查路径依赖个人经验的问题。我把它拆成两类 Agent：普通知识问答走 ReAct，复杂告警诊断走 LangGraph 的 Plan–Execute–Replan。底层通过 MCP 统一接日志、监控、只读 MySQL 和联网检索，RAG 使用本地 BGE 加 Milvus。除了功能，我重点做了工具重试和降级、SQL 只读边界、运行时指标，以及按模板隔离的 RAG/Agent 离线评测，避免只展示 Demo、无法量化效果。”

## 面试高频追问

### 为什么需要两套 Agent 工作流

ReAct 适合短链路、边思考边调用工具的开放问答，响应路径短；Plan–Execute–Replan 适合多证据排障，先生成计划，每执行一步都把结果写回状态，再决定继续、重规划或结束。代价是后者 LLM 调用次数更多，所以代码设置了最大步骤数和强制收敛条件。

### MCP 的价值是什么

MCP 把工具发现、参数 schema 和调用协议统一起来，Agent 不需要为日志、监控、数据库分别维护私有适配代码。项目使用 3 个 MCP Server 隔离不同能力，并在客户端拦截器里统一处理指数退避和指标采集。

### 重试如何避免放大故障

最多 3 次并使用指数退避与随机抖动；`PermissionError`、`ValueError`、`TypeError` 等调用方错误不重试，传输/服务错误可重试，最终只记录一次逻辑调用结果。API 入口使用信号量实施有界并发并在队列超时后返回 429，避免模型或下游故障时请求无限堆积；跨进程熔断仍是下一阶段工作。

### Hit@5 是什么

每条查询预先标记期望来源文档；如果该文档出现在前 5 个检索结果中，这条查询记为命中。Hit@5 衡量“相关证据是否被召回”，不等于最终答案正确率，所以项目同时记录 MRR@5、错误归因和 Agent 任务成功率。

### 这次 RAG 优化做了什么

先在 dev 集比较切分策略、Chunk Size、Overlap 和 Top-K；再锁定 plain/1200/0/Top-5，只在 test 集汇报最终结果。Bad case 主要集中在磁盘与延迟文档之间的排序混淆；plain 1200 减少过碎的标题分片，使完整排障上下文更容易进入前 5。

### MySQL 为什么称为只读

应用层只接受 SELECT/SHOW/DESCRIBE/EXPLAIN，拒绝多语句、注释和写入/管理关键字，同时限制 schema 和最大返回行数；数据库侧仍必须使用只读账号。面试时要主动强调：应用层校验是第二道防线，不能替代数据库权限。

### 项目最大的不足

目前知识库只有 10 份 Runbook，日志与监控是模拟服务；600 条查询和新增故障样例仍待人工复核，旧的 30 条 Agent 基线主要覆盖知识检索；整体语句覆盖率为 72.16%，真实生产告警与人工排障计时尚未补齐。会话可切换 PostgreSQL checkpoint，但指标与 admission control 仍是单进程状态，生产就绪接口会主动暴露这些阻断项。

## 针对岗位调整

- 投递 LLM/Agent 岗：把 RAG 数据切分、54 组实验、Hit@5/MRR@5、工作流收敛和失败归因放在前面。
- 投递后端岗：把 FastAPI/SSE、MCP 服务拆分、只读 SQL、重试降级、配置隔离和可观测性放在前面。
- 投递运维/AIOps 岗：把告警证据链、只读安全边界、日志/指标/历史事件联查和止损建议放在前面。

## 与 WindOS 项目的差异化写法

两个项目不能都写成“RAG + ReAct + 工具调用”。推荐把能力边界明确拆成：

| 维度 | WindOS | AIOps Copilot |
|---|---|---|
| 项目定位 | 垂直行业决策与组合优化 Agent | 通用 Agent 工程与运维诊断工作台 |
| 核心难点 | 多资源硬约束、排程可行性、LLM 与确定性求解器协作 | 多源证据采集、动态任务规划、工具协议与失败恢复 |
| 技术主线 | Function Calling + 多轮 Memory + OR-Tools 混合决策 | LangGraph 双工作流 + MCP 工具层 + RAG 评测与可观测性 |
| 重点指标 | 约束满足率、排程质量、求解耗时、历史计划回放 | Hit@K/MRR、任务成功率、工具成功率、P50/P95、失败归因 |
| 面试标签 | “懂业务约束和算法落地” | “懂 Agent 基础设施、评测和可靠性” |

### WindOS 推荐版本（用户提供数据，投递前需自行核验证据）

**WindOS｜福建海电运维海上风电智能排程系统｜核心开发｜2026.03–至今**  
**技术栈：** FastAPI / Function Calling / Milvus / SSE / OR-Tools

- 设计“LLM 意图解析、约束抽取与任务分解 + OR-Tools 确定性排程”的混合架构，将气象、船舶、人员与资源约束交由求解引擎计算，降低纯 LLM 数值幻觉和约束违规风险；自研多轮 Agent 执行框架，支持类型化工具注册、路由与分步编排。
- 整合故障代码、历史工单、算法文档与机组手册等知识源，覆盖 5 个厂商、10 类机型并构建 5,247 条 Milvus 索引，语义检索 Hit@5 达 92%；实现跨轮参数继承、指代消解与 Memory 压缩，提升连续排程会话的上下文一致性。
- 设计 6 类 SSE 事件，流式返回计划、工具调用、求解进度与最终排程，处理跨 Chunk JSON 恢复；基于 8 个风场、173 份历史计划进行离线回放，验证多约束场景下排程结果的可执行性。

这里建议继续补一个真正体现 OR-Tools 的数字，例如“可行解率、平均求解时延、相对人工计划的船期/工时/成本变化”。没有这组指标时，173 份回放更像数据规模，不足以证明优化效果。

### AIOps Copilot 推荐版本

**AIOps Copilot｜系统智能运维 Agent｜核心开发｜2025.10–2026.03**  
**技术栈：** LangGraph / DeepSeek V4 / RAG / MCP / Milvus / FastAPI

- 构建 ReAct 对话与 Plan–Execute–Replan 诊断双工作流，通过 MCP 统一发现并编排日志、监控、只读 MySQL、联网检索与 WINDOS 治理面等 19 个本地/MCP 工具，形成“告警解析—证据检索—动态规划—工具执行—重规划—建议生成”闭环；按任务将 DeepSeek V4 Flash 非思考模式用于工具链、V4 Pro 思考模式用于最终报告。
- 实现有界并发与 429 过载保护、分类重试/指数退避/抖动、熔断/半开探测/隔离舱、只读 SQL/schema/行数边界、API Key RBAC、请求 ID、OpenTelemetry 和 Prometheus 指标；SSE 支持事件 ID、游标、幂等回放、断线重试与取消传播；122 个自动化测试通过，语句覆盖率 72.16%。
- 构建 600 条可复现 RAG 查询，按模板隔离 train/dev/test，在 dev 上完成 54 组 Chunk/Overlap/Top-K 对照实验；锁定参数后在 120 条 held-out test 上将 BGE Hit@5 从 95.83% 提升至 98.33%、MRR@5 从 68.79% 提升至 76.06%，并对 30 条 Agent 任务进行 3 轮离线评测与失败归因。

### 排序与讲述策略

- 投 Agent/算法应用岗：WindOS 放前面，突出“LLM + 求解器”的混合决策；AIOps 放后面证明 Agent 工程、协议、评测和可靠性能力。
- 投 Agent 平台/后端岗：AIOps 放前面，突出 LangGraph、MCP、失败恢复、可观测性和测试；WindOS 作为垂直业务落地案例。
- 两个项目的第一句不要重复技术名词：WindOS 从“硬约束排程”讲起，AIOps 从“多源证据诊断和工具基础设施”讲起。
- 面试时分别准备一个失败案例：WindOS 讲不可行约束与求解降级，AIOps 讲工具超时/错误结果与重规划。这样比重复讲 RAG 参数更能拉开差距。

## 下一阶段达到“可写生产级”的条件

1. 人工复核 600 条标签，保留 reviewer、notes 和修改记录。
2. 新增 10–20 条脱敏真实告警，成对记录纯人工排查与 Agent 辅助排查耗时。
3. 在已加入的日志、监控、MySQL、Web Search 合成故障场景上运行并保存完整报告，再用真实后端故障演练补充外部有效性证据。
4. 当前覆盖率已达到 72.16%，已越过 70% 目标；已补真实 Milvus 容器契约与端到端取消传播测试，下一步继续覆盖容器重启和连接恢复。
5. 鉴权、角色控制、限流、请求指标和只读审计边界已经补齐；下一步接入真实可观测性后端、跨进程会话/指标、熔断与提示注入策略。

## 证据索引

| 面试问题 | 文件 |
|---|---|
| 两类工作流 | app/services/rag_agent_service.py、app/services/aiops_service.py |
| MCP 重试与降级 | app/agent/mcp_client.py、app/services/rag_agent_service.py |
| MySQL 只读规则 | mcp_servers/ops_server.py、tests/test_ops_server.py |
| RAG 数据和防泄漏切分 | evaluation/generate_datasets.py、tests/test_evaluation.py |
| dev 参数实验 | evaluation/rag_eval.py、evaluation/reports/rag_eval_local_20260715-222843.* |
| held-out test 对照 | evaluation/rag_baselines.py、evaluation/reports/rag_baselines_20260715-222630.* |
| Agent 指标 | evaluation/agent_eval.py、evaluation/reports/agent_eval_20260715-155944.* |
| 人工标签复核 | evaluation/review_labels.py、evaluation/label_review_server.py |
| 人工耗时记录 | evaluation/manual_timing.py |
