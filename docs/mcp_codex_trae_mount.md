# 在 Codex / Trae 中挂载 `aiops-mcp`

> 本页面向想把 **AIOps Copilot 作为领域知识后端**、由 Codex / Trae / Claude Desktop 等
> 主流 AI Coding Agent 当"大脑"的用户。挂载后，宿主 Agent 可以：
>
> 1. 让 `aiops-copilot` 按 Alertmanager 告警 top-k 匹配 **Skill**；
> 2. 拉出对应 **Runbook Wiki 片段**；
> 3. 生成可执行 **PlanStep DAG**（不走 LLM，恢复确定性）；
> 4. 在采集完证据后，让本项目渲染最终 **markdown 诊断报告**；
> 5. 生成 **告警指纹** 用于外部去重 / 归并。

`aiops-mcp` 是一个 **stdio MCP server**：宿主 Agent 通过标准输入输出直接与之对话，
**不需要额外起 HTTP 服务**，也不需要 Docker / Postgres / Redis / Milvus。默认零依赖，
即插即用。

---

## 1. 安装

```bash
# 只装 MCP 决策核心（约 30MB 冷装）
pip install 'aiops-copilot[mcp]'

# 或用 uv 完全免安装（推荐）
uvx --from aiops-copilot aiops-mcp --help
```

默认依赖：`pydantic` + `loguru` + `fastmcp`。若需要完整链路（FastAPI 服务端 / Milvus /
LLM），另行安装 `[server,rag,state,obs,llm]` 或直接 `[full]`。

---

## 2. Codex 挂载（CLI 版）

编辑 `~/.codex/config.toml`（或对应平台的用户配置），追加：

```toml
[mcp_servers.aiops-copilot]
command = "uvx"
args = ["--from", "aiops-copilot", "aiops-mcp"]

# 如果本地已 pip install 过，直接：
# command = "aiops-mcp"
# args = []
```

重启 Codex 后，在会话里就能看到 5 个新工具：

- `skill_match(alert_payload, top_k)`
- `skill_plan(skill_id, context)`
- `runbook_lookup(doc_ids, query, top_k)`
- `alert_fingerprint_tool(alert_payload)`
- `render_report(skill_match, plan, evidence_by_step, ...)`

---

## 3. Trae 挂载

Trae 的 MCP 配置面板里点 **Add server**，选择 stdio，填入：

| 字段 | 值 |
|---|---|
| Name | `aiops-copilot` |
| Command | `uvx` |
| Args | `--from aiops-copilot aiops-mcp` |
| Working dir | 你的告警 JSON 放置目录（可选） |

保存后即可在 Trae 的 chat 中通过 `@aiops-copilot skill_match` 等方式调工具。

---

## 4. Claude Desktop 挂载

编辑 `~/Library/Application Support/Claude/claude_desktop_config.json`：

```jsonc
{
  "mcpServers": {
    "aiops-copilot": {
      "command": "uvx",
      "args": ["--from", "aiops-copilot", "aiops-mcp"]
    }
  }
}
```

---

## 5. 典型工作流（宿主 Agent 视角）

设有一份 Alertmanager Webhook JSON（KafkaConsumerLagHigh 告警）：

```jsonc
{
  "alerts": [
    {
      "labels": {
        "alertname": "KafkaConsumerLagHigh",
        "service": "checkout",
        "consumergroup": "checkout-order",
        "topic": "orders"
      },
      "annotations": {
        "summary": "Kafka 消费者滞后过高",
        "description": "checkout-order 消费速度落后 orders"
      },
      "startsAt": "2026-08-23T12:00:00Z"
    }
  ]
}
```

**Step 1**：命中领域 Skill。

```
skill_match(alert_payload=<上文 JSON>, top_k=3)
→ {"matches": [{"skill_id": "aiops.kafka_lag", "score": 0.82, "reasons": [...],
               "runbook_refs": ["kafka_consumer_lag", "kafka_rebalance", "kafka_isr_shrink"]}]}
```

**Step 2**：抽 Runbook 片段作为 Prompt 上下文。

```
runbook_lookup(doc_ids=["kafka_consumer_lag"], query="Kafka 消费者滞后", top_k=2)
→ [{"doc_id": "...", "section_title": "止损与恢复", "content": "..."}, ...]
```

**Step 3**：生成计划 DAG（**不走 LLM**，恢复确定性）。

```
skill_plan(skill_id="aiops.kafka_lag", context={"labels": {"consumergroup": "checkout-order"}})
→ {"skill_id": "...", "steps": [{"step_id": "s1", "description": "...", "tool_hint": "..."}], ...}
```

**Step 4**：宿主 Agent 用**自己的** shell / MCP / 内置工具跑证据采集（`kubectl` /
`curl` / Prom API / 日志检索 …），把结果按 `{step_id: evidence}` 汇总。

**Step 5**：渲染 markdown 报告。

```
render_report(
  skill_match=<Step 3 结果>,
  plan=<Step 3 的 steps>,
  evidence_by_step={"s1": {"summary": "...", "status": "success"}, ...},
  trigger={"title": "KafkaConsumerLagHigh", "labels": {...},
           "fingerprint": alert_fingerprint_tool(alert_payload)["fingerprint"]},
)
→ "# 诊断报告\n\n## 触发\n..."
```

**Step 6**（可选）：将 `alert_fingerprint_tool` 的输出作为唯一键写入自建 incident 表，
用于外部去重 / 归并。

---

## 6. 设计边界

- **只暴露决策类工具**：Skill 匹配 / 计划渲染 / Runbook 抽取 / 指纹 / 报告渲染。
- **证据采集类工具**（HTTP / 日志 / Prom）**故意不暴露**——宿主 Agent 已经自带 shell
  和大量成熟工具，重复造轮子只会增加依赖成本、降低组合灵活性。
- **失败如实报告**：`skill_match` 未命中返回 `matches=[]`；`runbook_lookup` 缺失 wiki
  文件返回 `[]`；不允许 LLM 编造。
- **LLM 完全解耦**：`aiops-mcp` 主链路不 import `openai` / `langchain`。宿主用什么模型
  是宿主的自由。

---

## 7. 排障

| 症状 | 可能原因 | 解决 |
|---|---|---|
| 启动即报 `OptionalDependencyMissing: fastmcp` | 未装 `[mcp]` extra | `pip install 'aiops-copilot[mcp]'` |
| `skill_match` 全部返回 0 分 | 你的告警语言与 Skill 描述语言不同（例如全英） | 在 skill.yaml 里补充英文 symptoms / alert_names |
| `runbook_lookup` 返回空 | `doc_ids` 与 `aiops-docs/<id>.md` 文件名不一致 | 用 `skill_match` 返回的 `runbook_refs` 作为 doc_ids 输入 |
| stdio 挂上后宿主看不到工具 | `command` / `args` 拼写错误 | 手动跑一次 `aiops-mcp --transport stdio`，读 stderr |

---

## 8. 进一步阅读

- 项目定位与 AI-Native 架构：[README.md](../README.md)
- 完整重构方案：`.trae/documents/ai-native-minimal-refactor-plan.md`
- Runbook wiki 索引：[aiops-docs/](../aiops-docs/)
- Skill YAML 索引：[skills/aiops/](../skills/aiops/)
