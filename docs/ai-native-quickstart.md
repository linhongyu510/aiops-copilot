# AI-Native Quickstart

> 让 **Codex / Trae / Claude Desktop** 等主流 AI Coding Agent 用一行配置挂上 AIOps
> Copilot 的**领域知识后端**，或者用 `aiops-cli` 一条命令跑完整个 OnCall 链路。
> 三分钟从"命令行第一击"到"看到诊断报告"。

---

## 1. 三种使用姿势，一张对照表

| 姿势 | 适用场景 | 依赖档次 | 启动方式 |
|---|---|---|---|
| **`aiops-mcp` stdio** | Codex/Trae 交互式诊断，宿主 Agent 当"大脑" | `[mcp]` | 挂 `mcpServers` 配置 |
| **`aiops-cli` 一次性** | CI 回放告警，脚本化流水线，独立跑不联网 | 默认零依赖 | `aiops-cli oncall alert.json ...` |
| **完整 FastAPI 服务栈** | 长期驻留的诊断平台，多用户 / 多 domain | `[server,rag,state,obs,llm]` | `docker compose -f compose.yml up -d` |

三种姿势**共用同一套 `aiops_core` 决策实现**（Skill 匹配 / Runbook 抽取 / 计划渲染 /
指纹 / 报告），输出结构与语义完全一致。

---

## 2. 安装档次

```bash
# 默认零依赖内核：只带 pydantic + loguru + 标准库；跑得动 aiops-cli
pip install aiops-copilot

# 加 stdio MCP：需要挂载到 Codex/Trae 时
pip install 'aiops-copilot[mcp]'

# 全套：FastAPI + LangGraph + Milvus + LLM
pip install 'aiops-copilot[full]'

# 或用 uv 完全免安装
uvx --from aiops-copilot aiops-cli --help
uvx --from 'aiops-copilot[mcp]' aiops-mcp --help
```

**冷装体积对比**：默认 `pip install aiops-copilot` 约 30 MB；`[full]` 约 600 MB。

---

## 3. 姿势 A：`aiops-mcp` 挂进 Codex

编辑 `~/.codex/config.toml`：

```toml
[mcp_servers.aiops-copilot]
command = "uvx"
args = ["--from", "aiops-copilot[mcp]", "aiops-mcp"]
```

或本地已装：

```toml
[mcp_servers.aiops-copilot]
command = "aiops-mcp"
args = []
```

重启 Codex 后可以在会话里直接调这 5 个工具：

- `skill_match(alert_payload, top_k)`
- `skill_plan(skill_id, context)`
- `runbook_lookup(doc_ids, query, top_k)`
- `alert_fingerprint_tool(alert_payload)`
- `render_report(...)`

**样例 prompt**：

> 我收到一个 Alertmanager 告警（KafkaConsumerLagHigh），请用 `aiops-copilot` 命中
> Skill、拉 Runbook、然后基于你自己的 shell/kubectl 采集证据，最终调用 `render_report`
> 生成 markdown 诊断报告。

详细挂载配置（Trae / Claude Desktop）见
[docs/mcp_codex_trae_mount.md](./mcp_codex_trae_mount.md)。

---

## 4. 姿势 B：`aiops-cli` 单命令跑完

一站式诊断（无证据）：

```bash
aiops-cli oncall tests/fixtures/kafka_lag_alert.json
```

含证据回放（CI 友好）：

```bash
aiops-cli oncall tests/fixtures/kafka_lag_alert.json \
    --evidence file \
    --evidence-file tests/fixtures/kafka_lag_evidence.json \
    --output report.md
```

`aiops-cli` 还支持 `match` / `plan` / `runbook` / `fingerprint` / `env` 五个更细粒度
的子命令，详见 [docs/aiops_cli_usage.md](./aiops_cli_usage.md)。

---

## 5. 姿势 C：完整 FastAPI 服务栈

历史项目形态，功能最全（Web UI、SSE 流式诊断、审批闭环、Prometheus 指标）：

```bash
pip install 'aiops-copilot[full]'
docker compose -f compose.yml --profile full up -d --wait
```

FastAPI 主服务默认监听 `:9900`，MkDocs Wiki `:8010`，MCP 决策工具服务
`:18003/8004/8005`。详见 [README.md](../README.md) 的"快速启动"章节。

---

## 6. 一分钟对比 demo

三种姿势跑同一份告警，观察 **_诊断报告完全一致_** 只有配套依赖 / 交付载体不同：

```bash
# Cli
aiops-cli oncall tests/fixtures/kafka_lag_alert.json --output out-cli.md

# Mcp（打开 Codex，让宿主复述 skill_match + render_report 结果）
# 或用 python 内存客户端：
python - <<'PY'
import asyncio, json
from fastmcp import Client
from aiops_core.mcp_server import build_server
async def go():
    async with Client(build_server()) as c:
        alert = json.load(open("tests/fixtures/kafka_lag_alert.json"))
        m = (await c.call_tool("skill_match", {"alert_payload": alert, "top_k": 1})).data
        p = (await c.call_tool("skill_plan", {"skill_id": m["matches"][0]["skill_id"]})).data
        r = (await c.call_tool("render_report", {"skill_match": p, "plan": p["steps"]})).data
        open("out-mcp.md", "w").write(r)
asyncio.run(go())
PY

diff out-cli.md out-mcp.md   # 章节结构完全一致；差异仅在 CLI 侧带完整 trigger/fingerprint
```

---

## 7. 硬约束（三种姿势下均生效）

- **Planner 优先 Skill 而非 LLM 裸出**：命中 Skill 时永远走 Skill.steps 生成计划；
  未命中才允许交给宿主 LLM，且报告里明确标注"未命中"。
- **告警指纹字节级一致**：`alert_fingerprint()` 与旧版 `AlertmanagerTrigger.fingerprint()`
  在同一输入上产生完全相同的字节输出。
- **失败如实报告，禁 fake**：Skill 不命中返回空匹配；Runbook 缺失返回空片段；证据
  采集出错以 `status=error` 明示；报告渲染器不会为缺失章节编造内容。
- **`aiops-mcp` 只暴露决策类工具**：证据采集类工具（HTTP / 日志 / Prom）由宿主 Agent
  自带 shell 或 `[server]` extra 提供，避免重复造轮子。

---

## 8. 排障速查

| 症状 | 姿势 | 解决 |
|---|---|---|
| `ModuleNotFoundError: fastmcp` | A | `pip install 'aiops-copilot[mcp]'` |
| `未匹配到任何 Skill` | A/B | 用 `aiops-cli match` 看命中理由；补充 skill.yaml 的 symptoms |
| `pip install .` 后 `import app.main` 报 `OptionalDependencyMissing` | C | 需要 `pip install '.[server,llm]'` |
| `--evidence local` 全是 `skipped` | B | Skill 步骤的 `tool_hint` 不是本地可采集类型；改用 `--evidence file` |
| `aiops-cli env` 显示 `server: false` | 任意 | 该 extras 未装；本身不影响其他姿势 |

---

## 9. 进一步阅读

- Codex / Trae 挂载详解：[docs/mcp_codex_trae_mount.md](./mcp_codex_trae_mount.md)
- CLI 使用手册：[docs/aiops_cli_usage.md](./aiops_cli_usage.md)
- 完整重构方案（架构决策）：`.trae/documents/ai-native-minimal-refactor-plan.md`
- Runbook wiki 索引：[aiops-docs/](../aiops-docs/)
- Skill YAML 索引：[skills/aiops/](../skills/aiops/)
