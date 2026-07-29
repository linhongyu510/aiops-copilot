# WINDOS AI 自动运维演示

## 当前范围

本演示通过 Ops MCP 对 WINDOS 进行只读诊断，不允许取消任务、重启服务或写入业务数据。数据来自 `WINDOS_BASE_URL` 指向的真实 WINDOS API，返回中保留 `source=windos_live_api`、端点、HTTP 状态和延迟。

## 启动

1. 启动 WINDOS 后端（默认 `http://127.0.0.1:8002`）。
2. 在 AIOps Copilot 的 `.env` 配置 `WINDOS_BASE_URL`；受控模式使用 viewer 权限的 `WINDOS_API_KEY`。
3. 启动或重启 AIOps Copilot，打开 `http://127.0.0.1:9900`。

## 主演示提示词

> 请使用 windos_diagnose_overview 工具检查 WINDOS 当前运行状态，重点说明健康状态、生产就绪阻断项、排程队列和治理模式。严格基于工具结果，不要执行任何变更。

讲解顺序：

1. Agent 选择 `windos_diagnose_overview`；
2. MCP 并行查询健康、就绪、生产闸门、队列、Agent 指标和治理状态；
3. 单项 HTTP 503 或超时不会丢失其他证据；
4. 最终回答区分“API 可用”和“生产就绪”，并明确不执行变更；
5. 打开 `http://127.0.0.1:9900/api/metrics/tools` 展示工具成功率和 P50/P95。

## 当前本机可讲结论

- WINDOS API 健康且综合就绪；
- 当前为 `advisory` 模式，未达到生产就绪；
- 生产阻断项为 `runtime_mode`，应先进入 shadow 再进入 controlled；
- 异步排程队列关闭，队列端点返回 503 `asynchronous_scheduling_disabled`；
- 审计、认证、指标、备份等生产能力当前未启用或未声明；
- 这是对真实本机 API 的只读诊断，不是自动修复或生产 SLA 证明。

## 备用提示词

> 使用 windos_health 检查 WINDOS 为什么“服务可用但未达到生产就绪”，列出阻断项和警告。

> 使用 windos_queue_status 检查异步排程队列；如果功能关闭或返回错误，不要解释为“没有积压”。

> 使用 windos_agent_metrics 查看 WINDOS Agent 工具成功率和 P50/P95；没有调用记录时明确说明样本不足。

