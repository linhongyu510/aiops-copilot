"""End-to-end Agent benchmark against a running AIOps Copilot API."""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import statistics
import time
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATASET = ROOT / "evaluation" / "datasets" / "agent_tasks.jsonl"
REPORTS = ROOT / "evaluation" / "reports"


def percentile(values: list[float], value: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return round(ordered[max(0, math.ceil(value * len(ordered)) - 1)], 2)


def tool_deltas(before: dict, after: dict) -> tuple[list[str], int, int, dict[str, dict]]:
    names = set(before.get("tools", {})) | set(after.get("tools", {}))
    called: list[str] = []
    calls = 0
    successes = 0
    details: dict[str, dict] = {}
    for name in names:
        old = before.get("tools", {}).get(name, {})
        new = after.get("tools", {}).get(name, {})
        call_delta = new.get("calls", 0) - old.get("calls", 0)
        success_delta = new.get("successes", 0) - old.get("successes", 0)
        if call_delta > 0:
            called.append(name)
            calls += call_delta
            successes += max(0, success_delta)
            details[name] = {
                "calls": call_delta,
                "successes": max(0, success_delta),
                "failures": max(0, call_delta - success_delta),
            }
    return sorted(called), calls, successes, details


def call_event_delta(before: dict, after: dict) -> list[dict]:
    """Return value-free call schemas recorded after the pre-task snapshot."""
    before_sequence = max(
        (int(item.get("sequence", 0)) for item in before.get("recent_calls", [])),
        default=0,
    )
    return [
        item
        for item in after.get("recent_calls", [])
        if int(item.get("sequence", 0)) > before_sequence
    ]


def parameter_correctness(expected: dict[str, dict[str, str]], events: list[dict]) -> bool | None:
    if not expected:
        return None
    for tool, required_schema in expected.items():
        matching = [item for item in events if item.get("tool") == tool]
        if not matching:
            return False
        if not any(
            all(item.get("argument_schema", {}).get(key) == value for key, value in required_schema.items())
            for item in matching
        ):
            return False
    return True


def failure_reason(result: dict) -> str | None:
    if result.get("success"):
        return None
    if result.get("failure_reason"):
        return str(result["failure_reason"])
    error = str(result.get("error", "")).lower()
    if result.get("timed_out") or "timeout" in error or "timed out" in error:
        return "timeout"
    if not result.get("response_ok", True):
        return "response_error"
    if not result.get("tool_selection_ok", True):
        return "tool_not_called"
    if result.get("keyword_coverage", 1.0) < 0.5:
        return "keyword_mismatch"
    return "unexpected_error"


def summarize_tools(results: list[dict]) -> list[dict]:
    grouped: dict[str, list[dict]] = defaultdict(list)
    for result in results:
        names = set(result.get("expected_tools", [])) | set(result.get("tool_details", {}))
        for name in names:
            grouped[name].append(result)
    summaries = []
    for name in sorted(grouped):
        items = grouped[name]
        calls = sum(item.get("tool_details", {}).get(name, {}).get("calls", 0) for item in items)
        successes = sum(
            item.get("tool_details", {}).get(name, {}).get("successes", 0) for item in items
        )
        latencies = [item["latency_ms"] for item in items if item.get("latency_ms", 0) > 0]
        reasons: Counter[str] = Counter()
        for item in items:
            detail = item.get("tool_details", {}).get(name, {})
            if detail.get("failures", 0):
                reasons["timeout" if item.get("scenario") == "timeout" else "tool_error"] += detail[
                    "failures"
                ]
            elif reason := failure_reason(item):
                reasons[reason] += 1
        summaries.append(
            {
                "tool": name,
                "task_count": len(items),
                "calls": calls,
                "successes": successes,
                "success_rate": round(successes / calls, 4) if calls else 0.0,
                "p50_latency_ms": percentile(latencies, 0.50),
                "p95_latency_ms": percentile(latencies, 0.95),
                "failure_reasons": dict(sorted(reasons.items())),
            }
        )
    return summaries


def tool_selection_metrics(results: list[dict]) -> dict:
    """Micro-averaged selection quality and unnecessary-call count."""
    true_positive = false_positive = false_negative = 0
    unnecessary_calls = 0
    for result in results:
        expected = set(result.get("expected_tools", []))
        called = set(result.get("called_tools", []))
        true_positive += len(expected & called)
        false_positive += len(called - expected)
        false_negative += len(expected - called)
        unnecessary_calls += sum(
            int(detail.get("calls", 0))
            for name, detail in result.get("tool_details", {}).items()
            if name not in expected
        )
    precision = (
        true_positive / (true_positive + false_positive)
        if true_positive + false_positive
        else 1.0
    )
    recall = (
        true_positive / (true_positive + false_negative)
        if true_positive + false_negative
        else 1.0
    )
    return {
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "true_positive": true_positive,
        "false_positive": false_positive,
        "false_negative": false_negative,
        "unnecessary_tool_calls": unnecessary_calls,
    }


async def run_task(client: httpx.AsyncClient, base_url: str, task: dict, run_index: int) -> dict:
    before = (await client.get(f"{base_url}/api/metrics/tools")).json()
    started = time.perf_counter()
    response = await client.post(
        f"{base_url}/api/chat",
        json={"Id": f"eval-r{run_index}-{task['id']}", "Question": task["question"]},
    )
    latency_ms = (time.perf_counter() - started) * 1000
    payload = response.json()
    answer = payload.get("data", {}).get("answer") or ""
    after = (await client.get(f"{base_url}/api/metrics/tools")).json()
    called_tools, tool_calls, tool_successes, tool_details = tool_deltas(before, after)
    call_events = call_event_delta(before, after)
    arguments_ok = parameter_correctness(
        task.get("expected_argument_schemas", {}),
        call_events,
    )
    expected_tools = set(task.get("expected_tools", []))
    expected_keywords = task.get("expected_keywords", [])
    keyword_hits = [keyword for keyword in expected_keywords if keyword.lower() in answer.lower()]
    keyword_coverage = len(keyword_hits) / len(expected_keywords) if expected_keywords else 1.0
    tool_selection_ok = expected_tools.issubset(called_tools)
    response_ok = response.status_code == 200 and payload.get("code") == 200 and bool(answer)
    success = response_ok and tool_selection_ok and keyword_coverage >= 0.5
    return {
        "id": task["id"],
        "run": run_index,
        "category": task.get("category"),
        "scenario": task.get("scenario", "success"),
        "success": success,
        "response_ok": response_ok,
        "tool_selection_ok": tool_selection_ok,
        "called_tools": called_tools,
        "expected_tools": sorted(expected_tools),
        "tool_calls": tool_calls,
        "tool_successes": tool_successes,
        "tool_details": tool_details,
        "tool_call_schemas": call_events,
        "parameter_correct": arguments_ok,
        "keyword_coverage": round(keyword_coverage, 4),
        "matched_keywords": keyword_hits,
        "latency_ms": round(latency_ms, 2),
        "answer_preview": answer[:300],
    }


async def run(args: argparse.Namespace) -> dict:
    with args.dataset.open(encoding="utf-8") as handle:
        tasks = [json.loads(line) for line in handle if line.strip()]
    if args.sample:
        tasks = tasks[: args.sample]
    timeout = httpx.Timeout(args.timeout)
    results: list[dict] = []
    async with httpx.AsyncClient(timeout=timeout) as client:
        for run_index in range(1, args.runs + 1):
            for task in tasks:
                try:
                    results.append(
                        await run_task(client, args.base_url.rstrip("/"), task, run_index)
                    )
                    latest = results[-1]
                    print(
                        f"run={run_index}/{args.runs} task={task['id']} "
                        f"success={latest['success']} latency_ms={latest['latency_ms']}",
                        flush=True,
                    )
                except Exception as exc:
                    is_timeout = isinstance(exc, (httpx.TimeoutException, asyncio.TimeoutError))
                    results.append(
                        {
                            "id": task["id"],
                            "run": run_index,
                            "category": task.get("category"),
                            "scenario": task.get("scenario", "success"),
                            "success": False,
                            "error": str(exc),
                            "timed_out": is_timeout,
                            "failure_reason": "timeout" if is_timeout else "request_error",
                            "expected_tools": sorted(task.get("expected_tools", [])),
                            "latency_ms": 0.0,
                            "tool_calls": 0,
                            "tool_successes": 0,
                        }
                    )
    latencies = [item["latency_ms"] for item in results if item["latency_ms"] > 0]
    tool_calls = sum(item.get("tool_calls", 0) for item in results)
    tool_successes = sum(item.get("tool_successes", 0) for item in results)
    successes = sum(bool(item["success"]) for item in results)
    parameter_rows = [
        item for item in results if item.get("parameter_correct") is not None
    ]
    run_summaries = []
    for run_index in range(1, args.runs + 1):
        run_results = [item for item in results if item["run"] == run_index]
        run_successes = sum(bool(item["success"]) for item in run_results)
        run_summaries.append(
            {
                "run": run_index,
                "task_count": len(run_results),
                "task_successes": run_successes,
                "task_success_rate": round(run_successes / len(run_results), 4)
                if run_results
                else 0.0,
            }
        )
    tool_summaries = summarize_tools(results)
    all_failure_reasons: Counter[str] = Counter()
    for item in tool_summaries:
        all_failure_reasons.update(item["failure_reasons"])
    return {
        "generated_at": datetime.now().astimezone().isoformat(),
        "base_url": args.base_url,
        "unique_task_count": len(tasks),
        "runs": args.runs,
        "task_count": len(results),
        "task_successes": successes,
        "task_success_rate": round(successes / len(results), 4) if results else 0.0,
        "tool_calls": tool_calls,
        "tool_call_success_rate": round(tool_successes / tool_calls, 4) if tool_calls else 0.0,
        "tool_selection": tool_selection_metrics(results),
        "parameter_correctness": round(
            sum(bool(item["parameter_correct"]) for item in parameter_rows)
            / len(parameter_rows),
            4,
        )
        if parameter_rows
        else None,
        "average_latency_ms": round(statistics.mean(latencies), 2) if latencies else 0.0,
        "p50_latency_ms": percentile(latencies, 0.50),
        "p95_latency_ms": percentile(latencies, 0.95),
        "run_summaries": run_summaries,
        "tool_summaries": tool_summaries,
        "failure_reasons": dict(sorted(all_failure_reasons.items())),
        "results": results,
    }


def markdown_report(report: dict) -> str:
    return "\n".join(
        [
            "# End-to-End Agent Evaluation",
            "",
            f"- Generated: {report['generated_at']}",
            f"- Tasks: {report['task_count']}",
            f"- Unique tasks / repeated runs: {report['unique_task_count']} / {report['runs']}",
            f"- Task success rate: {report['task_success_rate']:.2%}",
            f"- Tool call success rate: {report['tool_call_success_rate']:.2%}",
            f"- Tool selection precision / recall: "
            f"{report['tool_selection']['precision']:.2%} / "
            f"{report['tool_selection']['recall']:.2%}",
            f"- Unnecessary tool calls: {report['tool_selection']['unnecessary_tool_calls']}",
            f"- Parameter correctness: "
            f"{report['parameter_correctness']:.2%}"
            if report["parameter_correctness"] is not None
            else "- Parameter correctness: not labeled",
            f"- Average latency: {report['average_latency_ms'] / 1000:.2f}s",
            f"- P50 / P95 latency: {report['p50_latency_ms'] / 1000:.2f}s / {report['p95_latency_ms'] / 1000:.2f}s",
            "",
            "## Per-run success rate",
            "",
            *[
                f"- Run {item['run']}: {item['task_successes']}/{item['task_count']} "
                f"({item['task_success_rate']:.2%})"
                for item in report["run_summaries"]
            ],
            "",
            "## Per-tool results",
            "",
            "| Tool | Success rate | P50 | P95 | Failure reasons |",
            "|---|---:|---:|---:|---|",
            *[
                f"| {item['tool']} | {item['success_rate']:.2%} | "
                f"{item['p50_latency_ms'] / 1000:.2f}s | {item['p95_latency_ms'] / 1000:.2f}s | "
                f"{json.dumps(item['failure_reasons'], ensure_ascii=False)} |"
                for item in report["tool_summaries"]
            ],
            "",
            "## Failure reasons",
            "",
            *(
                [f"- {name}: {count}" for name, count in report["failure_reasons"].items()]
                or ["- None"]
            ),
            "",
            "> Use reviewed labels and a representative task sample before copying these numbers to a resume.",
            "",
        ]
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--base-url", default="http://127.0.0.1:9900")
    parser.add_argument("--sample", type=int)
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--timeout", type=float, default=120.0)
    args = parser.parse_args()
    report = asyncio.run(run(args))
    REPORTS.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    json_path = REPORTS / f"agent_eval_{stamp}.json"
    md_path = REPORTS / f"agent_eval_{stamp}.md"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(markdown_report(report), encoding="utf-8")
    print(json_path)
    print(md_path)


if __name__ == "__main__":
    main()
