# Evaluation workflow

Run commands from the project root with `.venv\Scripts\python.exe`.

## RAG v2

RAG v2 使用 `aiops-runbook-v3` 的 40 份 Runbook 和 440 条 corpus-aware 查询。查询按文档整体分组为 train/dev/test=264/88/88，同一文档的改写不会跨集合；40 条 out-of-scope 查询用于无答案识别。

```powershell
.venv\Scripts\python.exe -m scripts.build_corpus_manifest
.venv\Scripts\python.exe -m evaluation.generate_rag_v2_dataset
.venv\Scripts\python.exe -m evaluation.rag_v2_eval --backend hashing --split dev
.venv\Scripts\python.exe -m evaluation.rag_v2_eval --backend local --split dev
.venv\Scripts\python.exe -m evaluation.rag_v2_ablation --split dev
.venv\Scripts\python.exe -m evaluation.rag_generation_eval --split dev --sample 20
# 可选 LLM Judge；结果仍必须人工抽检：
.venv\Scripts\python.exe -m evaluation.rag_generation_eval --split dev --sample 20 --llm-judge
```

消融顺序固定为 Dense Small、Dense Large、Rewrite、Multi-Query、HyDE、BM25、RRF、Reranker、Full。只有 dev 配置锁定后才允许运行 test：

```powershell
.venv\Scripts\python.exe -m evaluation.rag_v2_ablation --split test --confirm-locked-test
```

报告记录 corpus hash、固定模型 revision、配置、错误归因、P50/P95、峰值显存和 95% bootstrap 区间。LLM Judge 仅作辅助，报告强制保留人工抽检要求。所有 v2 标签当前仍为 `pending`，不能称为人工标注数据。

## RAG labels and baselines

```powershell
.venv\Scripts\python.exe evaluation/review_labels.py validate
.venv\Scripts\python.exe evaluation/review_labels.py precheck
.venv\Scripts\python.exe evaluation/label_review_server.py
.venv\Scripts\python.exe -m evaluation.rag_baselines
```

The review UI is available at `http://127.0.0.1:8765`. Machine precheck never
changes `review_status`; a real reviewer must approve or reject every row.

## Agent evaluation

Start the API and its MCP dependencies, then run:

```powershell
.venv\Scripts\python.exe -m evaluation.agent_eval --runs 3 --timeout 180
```

The evaluator uses a different session ID for every task/run pair and reports
task success, tool selection Precision/Recall, parameter correctness,
unnecessary calls, tool-call success, and average/P50/P95 latency.
It also emits per-tool success rate, end-to-end P50/P95, and normalized failure
reasons. The 12 `synthetic_fault_fixture` rows cover log, monitoring, read-only
MySQL, and web search success/timeout/error paths; they validate fault handling
and attribution, not production reliability or SLA claims.

## AIOps diagnosis evaluation (Plan-Execute-Replan)

`evaluation/aiops_eval.py` runs the diagnosis graph offline and in-process (no
HTTP API): the MCP tool layer is replaced by deterministic stub tools and
`retrieve_knowledge` by an offline stub, while the LLM defaults to the
configured real model. Use `--dry-run` for a no-key smoke run with a fake LLM:

```powershell
.venv\Scripts\python.exe evaluation/aiops_eval.py --dry-run
.venv\Scripts\python.exe evaluation/aiops_eval.py --sample 5 --timeout 600
```

The dataset `evaluation/datasets/aiops_diagnosis_cases.jsonl` holds 25
rule-seeded cases (`label_origin=rule_seeded`, all `review_status=pending`)
aligned with the `aiops-docs/` runbook categories (cpu/memory/disk/network/
db/k8s/mq/cert/redis/es/http). The report covers end-to-end success (response +
keyword coverage >= 0.5 + expected tools hit), planning quality (initial plan
step distribution, first-plan hit rate, replan rate, average executed steps,
step-budget adherence), degraded-plan rate, tool-call counts with selection
Precision/Recall against `expected_tools`, end-to-end P50/P95/P99 latency, and
per-case token/USD cost derived from `llm_metrics` snapshot diffs.

The tool layer is a deterministic stub, so tool-selection metrics reflect the
LLM's tool choice given canned data, not real troubleshooting ability. Dry-run
results only smoke-test the pipeline and must not be cited as model quality.
All labels remain `pending`; do not present them as human-reviewed data.

## Human investigation timing

Add only real alerts with a ticket, incident, or monitoring reference:

```powershell
.venv\Scripts\python.exe evaluation/manual_timing.py add --id INC-001 --title "CPU alert" --alert "raw alert text" --source-ref "ticket/INC-001"
.venv\Scripts\python.exe evaluation/manual_timing.py start --id INC-001 --reviewer "real reviewer"
.venv\Scripts\python.exe evaluation/manual_timing.py stop --id INC-001 --diagnosis "verified cause" --evidence "log/metric links or notes"
.venv\Scripts\python.exe evaluation/manual_timing.py summary
```

The summary is marked resume-eligible only after 10–20 measured cases. Never
enter estimated durations.
