# P0–P3 可靠性与成本验证（2026-07-24）

## 固化工程基线

- AIOps：135 passed，语句覆盖率 73.16%，Ruff 与 JavaScript 语法检查通过。
- 工具：19 个，其中 17 个 MCP 工具、2 个本地工具。
- 机器可读证据：`artifacts/project_evidence.json`、`artifacts/pytest.xml`、`artifacts/coverage.json`。
- 数字由 `scripts/project_evidence.py` 从测试产物和源码生成；代码变化后必须重新生成，不能手工沿用旧值。

## P0：联动与过载证据

三种 WINDOS 场景已保存到 `artifacts/reliability/windos_scenarios.json` 和同名 Markdown：

1. 正常访问：健康、就绪、治理和指标证据可读。
2. 单端点 503：明确标为合成故障注入，其余成功证据保留，证明部分失败降级。
3. WINDOS 不可达：保留来源、端点和异常类型，不伪造健康结论。

固定脚本对 1/4/8/16 并发进行 admission benchmark。16 并发、容量 8 时拒绝率为 50%，已接纳请求 P95 约 94 ms；原始数据、CSV 和图位于 `artifacts/reliability/load_benchmark.*`。该实验测量的是可重复的治理层负载，不应描述为生产容量。

## P1：持久化、追踪与故障恢复

- PostgreSQL checkpoint：RAG 对话与 Plan–Execute–Replan 诊断图在生命周期统一注入 LangGraph `AsyncPostgresSaver`；两个独立 saver 在测试图上交叉写读同一 thread，结果一致，证据为 `artifacts/reliability/checkpoint_consistency.json`。
- Windows 兼容：`app.run` 强制 Uvicorn 使用 SelectorEventLoop，已在 PostgreSQL required 模式真实启动，生产就绪检查返回 `persistent_sessions=postgres`。
- OpenTelemetry：FastAPI、HTTPX、MCP 和 WINDOS 依赖调用已接入 span 与 W3C header 传播；默认不配置 exporter，避免假称已部署集中式 Trace 后端。
- 依赖保护：实现 CLOSED/OPEN/HALF_OPEN 熔断、单探针半开恢复、隔离舱和错误分类；测试覆盖开路、拒绝、恢复以及调用方错误不计入熔断。
- Redis 协调：SSE 单调事件 ID、`Last-Event-ID` 游标回放、终态、幂等生产者租约与 admission control 可存入 Redis；双客户端验证了跨实例回放、重复拒绝和释放恢复，证据为 `artifacts/reliability/redis_coordination.json`。

## P2：规模、路由与错误预算

- 并发实验产出吞吐、拒绝率、P50/P95、下游错误和内存字段，脚本为 `evaluation/reliability_benchmark.py`。
- 三模型真实小样本对照：本地 Qwen3 8B 平均约 40.6 s、短指令 rubric 33%；DeepSeek V4 Flash 平均约 1.68 s、rubric 100%、估算成本约 $0.000077/轮；Pro 平均约 3.96 s，短关键词 rubric 不适合评价其长报告质量。当前路由结论是短工具链优先 Flash，复杂综合报告才使用 Pro。
- 工具指标已按 error class 聚合；`/api/metrics/reliability` 返回成功率、P95、错误预算和依赖 guard 状态，前端以 WINDOS 统一风格展示。

## P3：覆盖率、数据真实性与异构边界

- Replanner、真实 Milvus 容器契约、向量搜索、embedding、文件 API、checkpoint、Tracing、SSE 可靠性与取消传播测试将覆盖率提升至 73.16%。
- `evaluation/incident_review.py` 已生成 `evaluation/datasets/incident_cases_review.csv`。当前 6 条均为 `synthetic_fault_fixture` 且 `pending`，禁止描述成真实告警或人工标注；真实 reviewer 必须由人工填写。
- RTX 5070 Laptop GPU 可被系统识别，但当前 PyTorch 是 CPU 构建，CUDA 不可用。本地 BGE CPU 实测约 729.57 samples/s，证据为 `artifacts/reliability/embedding_benchmark.json`；不得在简历中写 GPU 性能优化。

## 仍需人工完成的唯一数据门禁

代码无法代替真实人工复核，也不能伪造生产告警。投递前应由本人审核 `incident_cases_review.csv`，填写 reviewer/notes 后同步回 JSONL；若未来引入真实告警，必须先脱敏并记录来源类型，再重新运行校验和评估。
