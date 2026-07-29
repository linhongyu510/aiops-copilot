# 2026-07-24 交付验证记录

> 注：本文档"WINDOS"一节为另一独立仓库的验证结果，与 AIOps Copilot 的测试数字分属两个项目，不可混用。

## AIOps Copilot

| 检查 | 结果 |
|---|---|
| `python -m pytest tests -q` | 122 passed，语句覆盖率 72.16%（包含真实 Milvus 容器契约检查） |
| `ruff check app mcp_servers evaluation tests` | passed |
| `uv lock --check` | passed，232 packages resolved |
| `node --check static/app.js` | passed |
| Uvicorn 启动冒烟 | v1.4.0 正常启动并连接 Milvus |
| `GET /live` | 200，返回请求 ID |
| `GET /ready` | 200，Milvus connected |
| `GET /api/metrics` | 200，Prometheus 指标可读 |
| `GET /` | 200，WINDOS 风格工作台和鉴权入口存在 |

`/production/readiness` 按预期返回 `ready_for_production=false`，阻断项为：

- authentication：本机配置未启用鉴权；
- persistent_sessions：默认内存模式；PostgreSQL 模式已完成真实启动与双实例一致性验证；
- distributed_tracing：代码已接入 OpenTelemetry，默认环境未配置 OTLP 后端；
- distributed_metrics：指标仍是进程内 registry。

这是设计边界检查通过，不是测试失败。启用 API Key、PostgreSQL checkpoint 与 OTLP exporter 可分别消除对应阻断项；进程内指标仍需接入集中式时序数据库。

## AIOps → WINDOS 真实联动

使用 `WINDOS_BASE_URL=http://127.0.0.1:8002` 直接执行 `windos_diagnose_overview`：

- WINDOS `/api/v1/health`：200；
- WINDOS `/api/v1/ready`：200，`ready=true`；
- 生产就绪：`false`，阻断项 `runtime_mode`；
- 当前治理模式：`advisory`；
- 异步排程队列：503，`asynchronous_scheduling_disabled`；
- Agent 指标和治理状态：200；
- 综合诊断保留所有成功证据，队列 503 未导致整体失败；
- `automatic_changes_permitted=false`，联动保持只读。

## WINDOS

| 检查 | 结果 |
|---|---|
| 后端 `python -m pytest -q` | 721 passed，12 skipped |
| 前端 `npm test` | 244 passed |
| 前端 `npm run build` | passed，93 modules transformed |

WINDOS 构建仍报告单个主 JavaScript chunk 约 2.20 MB、gzip 约 618 KB 的性能警告。它不影响本次构建通过，但后续应使用路由级动态导入和 Rollup `manualChunks` 拆分 Three.js/MapLibre/业务视图。由于 WINDOS 工作树存在大量用户未提交改动，本次只读验证，没有修改该仓库。

## 容器检查说明

Dockerfile 已通过 Dockerfile 解析阶段；Docker Desktop 29.2.1 可用。构建检查在拉取 `python:3.13-slim` 元数据时因当前网络无法连接 Docker Hub token 服务而终止，尚未进入镜像构建步骤。这是外部网络条件，不计入代码测试通过结论；网络恢复后执行：

```powershell
docker build --check .
docker build -t aiops-copilot:1.4.0 .
```

## 2026-07-27 升级验证（追加）

可靠性升级批次（MCP 执行超时与错误分类重试、SSE replay TTL/LRU 驱逐与并发 409 去重、Milvus 真实 RPC 健康检查、chat 路径总预算与 recursion_limit）完成后：

| 检查 | 结果 |
|---|---|
| `pytest tests --no-cov` | 134 passed, 1 skipped（跳过项为需环境变量开启的 Milvus 容器集成测试） |
| `ruff check app mcp_servers evaluation tests` | passed |
| Uvicorn 启动冒烟 | 正常启动，`/live` `/health` `/ready` 均 200，Milvus 经真实 RPC（`utility.get_server_version()`）检查连接正常 |

注：`uv remove aiohttp langchain-qwq` 已更新 pyproject.toml 与 uv.lock；`.venv` 中 aiohttp 残留文件需在开发服务停止后执行一次 `uv sync` 完成物理卸载。
