# AI-Native Quickstart

> 本页是 wiki 侧栏的**跳转索引**。AI-Native 用法（`aiops-mcp` stdio + `aiops-cli`
> 一次性入口）的完整说明位于项目根的 `docs/` 目录，独立于 wiki 语料，不进 RAG。

## 一键上手

```bash
# 默认零依赖：跑一次完整 OnCall 链路
pip install aiops-copilot
aiops-cli oncall tests/fixtures/kafka_lag_alert.json

# 挂给 Codex（编辑 ~/.codex/config.toml）
[mcp_servers.aiops-copilot]
command = "uvx"
args = ["--from", "aiops-copilot[mcp]", "aiops-mcp"]
```

## 三种姿势

| 姿势 | 用途 | 依赖 |
|---|---|---|
| `aiops-mcp` stdio | Codex/Trae 挂载，宿主 Agent 当"大脑" | `[mcp]` |
| `aiops-cli` 一次性 | CI 回放告警、脚本化流水线 | 默认零依赖 |
| 完整 FastAPI 服务栈 | 长期驻留的诊断平台 | `[full]` |

## 完整文档（在项目仓库 `docs/` 目录下）

- **`docs/ai-native-quickstart.md`**：三种姿势对比 & 三分钟上手
- **`docs/mcp_codex_trae_mount.md`**：Codex / Trae / Claude Desktop 挂载配置
- **`docs/aiops_cli_usage.md`**：`aiops-cli` 使用手册

## 硬约束（三种姿势均生效）

- Planner 优先 Skill 而非 LLM 裸出计划
- 告警指纹与旧版 `AlertmanagerTrigger.fingerprint()` 字节级一致
- Skill 不命中 / Runbook 缺失 / 证据采集失败 → 如实报告，禁 fake
- `aiops-mcp` 只暴露决策类工具，证据采集类交给宿主 Agent 自带的 shell / MCP
