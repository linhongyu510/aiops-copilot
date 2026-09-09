# AIOps Runbook Wiki

> **40 篇生产级 OnCall 运维预案**。这里同时是 [AIOps Copilot](https://github.com/) 的 RAG 语料库：`aiops-docs/*.md` 经 Milvus 向量化后供 Agent 检索。

---

## 使用方式

=== "人肉查阅"

    - 左侧按分类导航到具体场景，或右上角搜索关键字（支持中文分词）
    - 每篇 runbook 遵循统一骨架：**识别信号 / 排查顺序 / 常见根因 / 快速缓解 / 长期治理 / 验证清单**
    - 有紧急告警时，先按 **识别信号** 快速匹配当前症状，再进入 **排查顺序**

=== "Agent 检索"

    - Alert / Chat / Scheduled / SlashCommand 触发时，Skill Registry 会按告警名与关键词匹配 runbook
    - 变更操作生成的 `ActionProposal` 会通过 `skill_id / skill_step_id` 反向归因到具体 runbook
    - 文档变更后需重新构建索引：`POST /api/index_directory` 或运行 `python scripts/build_corpus_manifest.py`

=== "贡献流程"

    1. 编辑 `aiops-docs/*.md`
    2. 提 PR，CI 会自动重建 MkDocs 站点
    3. 合并后触发 `build_corpus_manifest.py`，`CORPUS_MANIFEST.json` 的 `corpus_hash` 会更新
    4. 运维手工触发一次 `POST /api/index_directory` 完成 Milvus 重新索引

---

## 分类速览

<div class="grid cards" markdown>

- :material-kubernetes: **Kubernetes** · 9 篇

    Pod / HPA / Ingress / 网络策略 / 节点压力 / OOM / 滚动升级

- :material-database: **数据库** · 4 篇

    MySQL 慢查询 · 锁等待 · 主从延迟 · 连接池耗尽

- :material-message-processing: **消息队列** · 5 篇

    Kafka lag / ISR / rebalance · RabbitMQ 积压 · 通用积压

- :material-memory: **缓存** · 2 篇

    Redis 内存压力 · Redis 慢日志

- :material-linux: **Linux 系统** · 7 篇

    CPU / 内存 / 磁盘 / IO Wait / Load / inode / 文件描述符

- :material-lan: **网络** · 4 篇

    DNS 故障 · 丢包 · 端口连通性 · 证书过期

- :material-application-brackets: **应用层** · 6 篇

    内存泄漏 · GC pause · 线程池耗尽 · 慢响应 · 5xx · 限流

- :material-shield-alert: **治理与依赖** · 2 篇

    熔断打开 · 服务不可用

</div>

---

## 语料版本

| 字段 | 值 |
|---|---|
| Corpus ID | `aiops-runbook-v3` |
| 文档数量 | 40 |
| 语言 | 中文（zh-CN） |
| 分片策略 | markdown-header, chunk_size=400 tokens, overlap=64 |
| 向量模型 | `BAAI/bge-large-zh-v1.5` (dim=1024, COSINE) |
| Reranker | `BAAI/bge-reranker-v2-m3` |
| 检索 | 8 分支 → RRF 融合 → top-K=5 |

详见 [`aiops-docs/CORPUS_MANIFEST.json`](https://github.com/)。

---

## 关联能力

- **Skill 匹配**：`GET /api/skills/match?q=...` 会加权匹配 runbook 的关键词与 alertname
- **事件接入**：`POST /api/events/generic` 触发诊断时会自动召回相关 runbook
- **变更提案**：`ActionProposal.skill_id` 归因到触发该变更的 Skill 与 runbook
- **诊断报告**：Replanner 输出的 `DiagnosisOutcome` 会引用 runbook 的 **验证清单** 作为回归验收依据
