# Evaluation workflow

Run commands from the project root with `.venv\Scripts\python.exe`.

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
task success, tool selection, tool-call success, and average/P50/P95 latency.
It also emits per-tool success rate, end-to-end P50/P95, and normalized failure
reasons. The 12 `synthetic_fault_fixture` rows cover log, monitoring, read-only
MySQL, and web search success/timeout/error paths; they validate fault handling
and attribution, not production reliability or SLA claims.

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
