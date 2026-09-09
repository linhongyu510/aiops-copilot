# RAG 2.0 五分钟演示脚本

## 演示前检查

1. 使用 `.env.example` 的 BGE Large、1024 维、Milvus 2.6 和版本化 collection 配置。
2. 运行 `python quickstart.py`，确认 Runbook 上传完成且 `aiops_kb_current` alias 已发布。
3. 打开 Web 工作台，进入“RAG 检索实验台”；同时准备 `/api/metrics/retrieval`。

## 主演示

输入：

> Kubernetes Pod 出现 OOMKilled，如何区分容器 limit、节点压力和应用泄漏？先收集什么证据？

按顺序讲解：

1. Rewrite 消除口语和指代，Multi-Query 分别覆盖症状、原因和处置，HyDE 构造仅用于检索的假设 Runbook。
2. 展示 6 路 Dense 和 2 路 BM25 的候选数与 Top chunk ID；强调原始 COSINE 与 BM25 分数没有直接相加。
3. 展示 RRF Top-30 排名，再展示 BGE Reranker 排名变化。
4. 展示来源配额、近重复过滤后的 Top-5，以及回答中的 `[1]…[5]` 引证。
5. 打开检索指标，说明 P50/P95、候选数、cache hit、无答案与降级计数。

## 无答案演示

输入：

> 公司员工年假审批制度是什么？

预期：没有足够内部证据时明确拒答，不使用模型常识补全公司制度。

## 降级演示

临时将 `RAG_RERANKER_MODEL` 设置为不存在的本地路径并重启 API。再次执行主问题：

- 检索仍返回 RRF Top-5；
- trace 中出现 `reranker` 降级；
- 恢复配置后重新启动，不需要重建 Dense/BM25 索引。

不要在共享环境修改配置；面试现场可直接展示单元测试
`test_bm25_and_reranker_failures_keep_dense_results` 代替故障注入。

## 评测证据

最后展示：

- `aiops-docs/CORPUS_MANIFEST.json`：语料 hash、模型 revision、切分与检索合同；
- `evaluation/datasets/rag_v2_queries.jsonl`：440 条规则构造查询及 pending 人工复核状态；
- `evaluation/rag_v2_ablation.py`：固定消融顺序、严格质量门禁和 Test 单次运行保护；
- `artifacts/project_evidence.json`：测试、覆盖率和 36 个工具的自动证据。

结束时主动说明：真实 BGE/Reranker Test 报告与人工复核完成前，不在简历中填写 v2 提升百分比。
