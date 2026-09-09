# `aiops-cli` 一次性本地 OnCall 入口

> 单命令跑通完整 OnCall 链路。**默认零外部依赖**（仅 `pydantic` + `loguru`）——不需要
> Docker / Postgres / Redis / Milvus / 网络也能出诊断报告。适合本地演练、CI 回放、
> 或作为 Codex Task 的一步 shell。

---

## 1. 安装

```bash
# 只装 CLI 决策核心（约 30MB 冷装）
pip install aiops-copilot

# 或用 uv 完全免安装
uvx --from aiops-copilot aiops-cli --help
```

安装后即可 `aiops-cli` 或 `python -m aiops_core.cli`。

---

## 2. 子命令一览

```
aiops-cli match       <alert.json> [--top-k 3]
aiops-cli plan        <alert.json> [--skill-id ...] [--top-k 1]
aiops-cli runbook     <doc_id ...> --query "..." [--top-k 3]
aiops-cli fingerprint <alert.json>
aiops-cli oncall      <alert.json>
                       [--evidence none|file|local]
                       [--evidence-file path.json]
                       [--skill-id ...]
                       [--top-k 1] [--runbook-top-k 3]
                       [--output report.md]
aiops-cli env
```

---

## 3. 一站式 `oncall`

```bash
aiops-cli oncall tests/fixtures/kafka_lag_alert.json
```

按顺序执行：**Skill 匹配 → 计划渲染 → Runbook 抽取 → 证据采集 → markdown 报告**。
所有环节零 LLM、可离线。

### 三种证据源

| `--evidence` | 用途 | 依赖 |
|---|---|---|
| `none`（默认） | 只输出 Skill / Plan / Runbook 提示；证据由宿主 Agent 自采 | 零 |
| `file` | 从 JSON 回放证据，格式 `{step_id: raw}` | 零 |
| `local` | 零依赖本地后端采证据（`http_probe` / `read_file` / `shell` 白名单） | 零 |

**file 模式示例**：

`evidence.json`：

```json
{
  "sk:lag_trend": {
    "tool": "prom_query",
    "summary": "lag 增长率 240 msg/s，突增",
    "status": "warn"
  },
  "sk:consumer_log": "发现 rebalance 超时 3 次"
}
```

```bash
aiops-cli oncall alert.json --evidence file --evidence-file evidence.json
```

**local 模式**：Skill 步骤的 `tool_hint` 命中 `http_probe` / `read_file` / `shell`
时才有效；其他 hint（如 `prom_query`）自动 fall back 到 `skipped`，宿主 Agent 可
后续补齐。shell 命令走[白名单](../aiops_core/evidence/local_backends.py)，禁止 `rm`
等破坏性操作。

---

## 4. 输出到文件

```bash
aiops-cli oncall alert.json --evidence file --evidence-file evidence.json --output report.md
```

`--output` 存在时 stdout 保持干净，方便配合 `>>` 组装工作流。

---

## 5. 与 `aiops-mcp` 的分工

| 场景 | 用 CLI | 用 MCP |
|---|---|---|
| CI 中回放告警、比对报告 | ✅ | 不合适 |
| Codex / Trae 交互式诊断 | 手动跑 | ✅ 挂载后自然工具 |
| shell 脚本一步跑完 | ✅ | 需 stdio 客户端 |
| 需要与其他 Agent 组合流水线 | 组合成 `pipe` | 交由宿主编排 |

两者共享同一套 `aiops_core` 决策实现，输出结构完全一致；CLI 相当于把 MCP 工具打包成
命令行序列，便于脚本化。

---

## 6. 排障

| 症状 | 可能原因 | 解决 |
|---|---|---|
| `未匹配到任何 Skill` | 告警语言与 Skill 描述语言不同，或 skill.yaml 缺失 | 补充 symptoms/alert_names 或运行 `aiops-cli match` 看命中理由 |
| `--evidence local` 全部 `skipped` | Skill 步骤的 `tool_hint` 不是本地可采集类型 | 用 `--evidence file` 或让宿主 Agent 采集 |
| shell 命令报 `denied` | 命令不在白名单 | 显式修改 `aiops_core/evidence/local_backends.py::_SHELL_ALLOWLIST` |
| `报告已写入：xxx` 但文件是空 | 通常是 skill 未命中导致 plan 为空 | 检查 `aiops-cli match` 输出 |

---

## 7. 进一步阅读

- Codex / Trae 挂载：[docs/mcp_codex_trae_mount.md](./mcp_codex_trae_mount.md)
- 完整重构方案：`.trae/documents/ai-native-minimal-refactor-plan.md`
- Runbook wiki 索引：[aiops-docs/](../aiops-docs/)
- Skill YAML 索引：[skills/aiops/](../skills/aiops/)
