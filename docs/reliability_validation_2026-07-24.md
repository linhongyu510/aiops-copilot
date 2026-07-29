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

固定脚本通过真实网络端口请求 `/api/chat_stream`，对 1/4/8/16 并发各执行 8 轮 SSE。容量 8、排队预算 20 ms、确定性下游耗时 80 ms 时，16 并发共 128 请求，71 个完成、57 个返回 429（拒绝率 44.53%），已接纳请求 P95 为 369.28 ms，下游合成错误率为 5.63%，进程 RSS 增量约 1 MiB。原始 JSON、CSV 和图位于 `artifacts/reliability/load_benchmark.*`。该实验验证真实 HTTP/SSE、middleware 与 admission-control 路径，但模型响应为确定性桩，不应描述为生产容量或真实模型吞吐。

## P1：持久化、追踪与故障恢复

- PostgreSQL checkpoint：RAG 对话与 Plan–Execute–Replan 诊断图在生命周期统一注入 LangGraph `AsyncPostgresSaver`；两个独立 saver 在测试图上交叉写读同一 thread，结果一致，证据为 `artifacts/reliability/checkpoint_consistency.json`。
- Windows 兼容：`app.run` 强制 Uvicorn 使用 SelectorEventLoop，已在 PostgreSQL required 模式真实启动，生产就绪检查返回 `persistent_sessions=postgres`。
- OpenTelemetry：FastAPI、MCP ASGI 与 HTTPX 使用 W3C header 传播；只读 Trace 探针在同一 trace 中包含 `GET /api/observability/trace-probe`、`mcp.windos_health` 与 `windos.http`。本次启动 Collector、Jaeger、Prometheus、Grafana 后验证通过，证据为 `artifacts/reliability/observability_stack.json`；默认环境仍关闭 exporter。
- 依赖保护：实现 CLOSED/OPEN/HALF_OPEN 熔断、单探针半开恢复、隔离舱和错误分类；测试覆盖开路、拒绝、恢复以及调用方错误不计入熔断。
- Redis 协调：SSE 单调事件 ID、`Last-Event-ID` 游标回放、终态、幂等生产者租约与 admission control 可存入 Redis；双客户端验证了跨实例回放、重复拒绝和释放恢复，证据为 `artifacts/reliability/redis_coordination.json`。

## P2：规模、路由与错误预算

- 真实 HTTP/SSE 并发实验产出吞吐、拒绝率、全量与已接纳 P50/P95、下游错误、SSE 事件数和内存字段，脚本为 `evaluation/http_sse_benchmark.py`；`evaluation/reliability_benchmark.py` 仅保留为纯治理层微基准。
- 三模型真实小样本对照：本地 Qwen3 8B 选取 3 条代表样本，契约通过 3/3，平均 8.06 s、P95 17.07 s；DeepSeek V4 Flash 跑完 9 条，短诊断 6/6、复杂报告 2/3，平均 2.46 s、P95 5.05 s，9 次估算总成本 $0.000369；V4 Pro 只跑 3 条复杂报告，3/3 通过，平均 58.41 s、P95 60.18 s，3 次估算总成本 $0.006317。路由阈值据此固定为：`short_triage → Flash`，`complex_report → Pro`。通过率仅表示必需关键词齐全且未命中危险措辞，不是人工评价的答案准确率；原始逐条结果位于 `artifacts/reliability/model_routing_benchmark.json`。
- 工具指标已按 error class 聚合；`/api/metrics/reliability` 返回成功率、P95、错误预算和依赖 guard 状态，前端以 WINDOS 统一风格展示。

## P3：覆盖率、数据真实性与异构边界

- Replanner、真实 Milvus 容器契约、向量搜索、embedding、文件 API、checkpoint、Tracing、SSE 可靠性与取消传播测试将覆盖率提升至 73.16%。
- `evaluation/incident_review.py` 已生成 `evaluation/datasets/incident_cases_review.csv`。当前 6 条均为 `synthetic_fault_fixture` 且 `pending`，禁止描述成真实告警或人工标注；真实 reviewer 必须由人工填写。
- `evaluation/import_anonymized_alerts.py` 提供授权确认、字段校验和常见敏感模式拦截，`evaluation/review_status.py` 将当前可声明范围固化到 `artifacts/reliability/human_review_status.json`。仓库当前没有获授权的脱敏真实告警，不能为了完成指标而生成伪样本。
- RTX 5070 Laptop GPU 可被系统识别，但当前 PyTorch 是 CPU 构建，CUDA 不可用。本地 BGE CPU 实测约 729.57 samples/s，证据为 `artifacts/reliability/embedding_benchmark.json`；不得在简历中写 GPU 性能优化。

## 仍需人工完成的唯一数据门禁

代码无法代替真实人工复核，也不能伪造生产告警。投递前应按 `evaluation/datasets/REVIEW_GUIDE.md` 审核复核表，填写 reviewer/notes 后同步回 JSONL；若未来引入真实告警，须由数据所有者授权并先脱敏，再通过 `evaluation/import_anonymized_alerts.py --acknowledge-authorized-source` 导入、人工复核并重新生成状态证据。
