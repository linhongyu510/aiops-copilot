"""Offline evaluation of the Plan-Execute-Replan diagnosis graph.

Runs the AIOps diagnosis graph in-process (no HTTP API): the MCP tool layer is
replaced by deterministic stub tools and ``retrieve_knowledge`` by an offline
stub, while the LLM defaults to the configured real model. ``--dry-run``
swaps in a fake LLM for a no-key smoke run.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import statistics
import sys
import time
import uuid
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableLambda
from langchain_core.tools import tool

from app.config import config
from app.observability.llm_metrics import diff_snapshots, llm_metrics
from app.services.aiops_service import (
    NODE_EXECUTOR,
    NODE_PLANNER,
    NODE_REPLANNER,
    AIOpsService,
)
from evaluation.agent_eval import percentile, tool_selection_metrics

DEFAULT_DATASET = ROOT / "evaluation" / "datasets" / "aiops_diagnosis_cases.jsonl"
REPORTS = ROOT / "evaluation" / "reports"

# app.agent.aiops.__init__ 将节点函数导出并遮蔽模块名，需显式导入模块
PLANNER_MODULE = importlib.import_module("app.agent.aiops.planner")
EXECUTOR_MODULE = importlib.import_module("app.agent.aiops.executor")
REPLANNER_MODULE = importlib.import_module("app.agent.aiops.replanner")
NODE_MODULES = (PLANNER_MODULE, EXECUTOR_MODULE, REPLANNER_MODULE)


class ToolRecorder:
    """记录桩工具调用顺序的共享列表。"""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def record(self, name: str) -> None:
        self.calls.append(name)

    def snapshot(self) -> int:
        return len(self.calls)

    def delta(self, before: int) -> list[str]:
        return self.calls[before:]


def build_stub_tools(recorder: ToolRecorder) -> list:
    """构造确定性桩工具：名称对齐真实 MCP 工具，返回固定的假监控数据。"""

    @tool
    async def query_cpu_metrics(service_name: str = "eval-service") -> str:
        """查询服务 CPU 使用率指标（评测桩）"""
        recorder.record("query_cpu_metrics")
        return f"服务 {service_name} CPU 使用率 92%，主要由进程 java(pid=1234) 占用，负载持续上升"

    @tool
    async def query_memory_metrics(service_name: str = "eval-service") -> str:
        """查询服务内存使用率指标（评测桩）"""
        recorder.record("query_memory_metrics")
        return f"服务 {service_name} 内存使用率 91%，堆内存持续增长，疑似内存泄漏"

    @tool
    async def prom_query(query: str = "up") -> str:
        """执行 PromQL 即时查询（评测桩）"""
        recorder.record("prom_query")
        return f'查询 "{query}" 结果：磁盘使用率 95%，丢包率 8%，队列积压 120000，集群状态 RED（存在未分配分片）'

    @tool
    async def prom_query_range(query: str = "up", hours: int = 1) -> str:
        """执行 PromQL 区间查询（评测桩）"""
        recorder.record("prom_query_range")
        return f'区间查询 "{query}"（近 {hours} 小时）：指标呈持续上升趋势'

    @tool
    async def prom_active_alerts() -> str:
        """查询当前活跃告警（评测桩）"""
        recorder.record("prom_active_alerts")
        return "活跃告警：HighCPUUsage(critical, order-service)，ServiceDown(warning, inventory)，可用性下降"

    @tool
    async def search_topic_by_service_name(service_name: str = "eval-service") -> str:
        """根据服务名查找 CLS 日志主题（评测桩）"""
        recorder.record("search_topic_by_service_name")
        return json.dumps({"topics": [{"topic_id": "topic-eval-001"}]}, ensure_ascii=False)

    @tool
    async def search_log(topic_id: str = "topic-eval-001") -> str:
        """按 topic_id 查询 CLS 日志（评测桩）"""
        recorder.record("search_log")
        return f"[{topic_id}] ERROR 5xx 错误率上升：upstream connect timeout；ERROR 数据库连接池耗尽"

    @tool
    async def k8s_list_pods(namespace: str = "default") -> str:
        """列出 K8s Pod（评测桩）"""
        recorder.record("k8s_list_pods")
        return json.dumps(
            {
                "namespace": namespace,
                "pods": [
                    {"name": "checkout-0", "status": "CrashLoopBackOff", "restarts": 12},
                    {"name": "search-0", "status": "OOMKilled", "restarts": 5},
                    {"name": "api-0", "status": "Pending", "restarts": 0},
                ],
            },
            ensure_ascii=False,
        )

    @tool
    async def k8s_describe_workload(name: str = "checkout", namespace: str = "default") -> str:
        """描述 K8s 工作负载详情（评测桩）"""
        recorder.record("k8s_describe_workload")
        return f"{namespace}/{name}：limits.memory=512Mi，实际占用 780Mi，lastState=OOMKilled"

    @tool
    async def k8s_get_events(namespace: str = "default") -> str:
        """查询 K8s 事件（评测桩）"""
        recorder.record("k8s_get_events")
        return f"[{namespace}] FailedScheduling: 节点资源不足；BackOff: 容器反复重启；FailedCreatePodSandBox"

    @tool
    async def k8s_get_logs(pod: str = "checkout-0", namespace: str = "default") -> str:
        """读取 K8s Pod 日志（评测桩）"""
        recorder.record("k8s_get_logs")
        return f"[{namespace}/{pod}] FATAL 配置缺失导致启动失败，容器退出码 1，进入重启循环"

    @tool
    async def k8s_rollout_status(deployment: str = "api-server", namespace: str = "default") -> str:
        """查询 K8s 发布状态（评测桩）"""
        recorder.record("k8s_rollout_status")
        return f"deployment {namespace}/{deployment} 发布卡住：新副本就绪探针失败，建议回滚"

    @tool
    async def docker_container_stats(container: str = "eval-container") -> str:
        """查询容器资源占用（评测桩）"""
        recorder.record("docker_container_stats")
        return f"容器 {container}：CPU 85%，内存 1.8GiB/2GiB"

    @tool
    async def probe_http(url: str = "http://eval.local") -> str:
        """探测 HTTP/TCP 连通性（评测桩）"""
        recorder.record("probe_http")
        return f"探测 {url}：端口连接超时，健康检查失败，服务不可用"

    @tool
    async def resolve_dns(host: str = "api.example.com") -> str:
        """解析 DNS 记录（评测桩）"""
        recorder.record("resolve_dns")
        return f"DNS 解析 {host} 失败：NXDOMAIN，权威 DNS 服务器无响应"

    @tool
    async def inspect_tls_certificate(host: str = "api.example.com", port: int = 443) -> str:
        """检查 TLS 证书有效期（评测桩）"""
        recorder.record("inspect_tls_certificate")
        return f"{host}:{port} 证书将于 7 天后过期，颁发者 Let's Encrypt，建议尽快轮换"

    @tool
    async def redis_info(section: str = "memory") -> str:
        """查询 Redis INFO（评测桩）"""
        recorder.record("redis_info")
        return f"Redis {section}：used_memory 超过 maxmemory 的 92%，淘汰策略 allkeys-lru，已淘汰 1024 个键"

    @tool
    async def redis_slowlog(limit: int = 20) -> str:
        """查询 Redis 慢查询日志（评测桩）"""
        recorder.record("redis_slowlog")
        return f"最近 {limit} 条慢查询：KEYS *（耗时 850ms）、HGETALL big-hash（耗时 320ms）"

    @tool
    async def mysql_read_query(sql: str = "SELECT 1") -> str:
        """执行只读 MySQL 查询（评测桩）"""
        recorder.record("mysql_read_query")
        return (
            f'执行 "{sql}"：活跃连接 198/200（连接池打满），慢查询 order 表全表扫描，'
            "锁等待事务 12 个，主从复制延迟 320 秒"
        )

    @tool
    async def web_search(query: str = "") -> str:
        """搜索互联网参考资料（评测桩）"""
        recorder.record("web_search")
        return f'搜索 "{query}"：官方文档建议检查资源限制与配置项'

    return [
        query_cpu_metrics,
        query_memory_metrics,
        prom_query,
        prom_query_range,
        prom_active_alerts,
        search_topic_by_service_name,
        search_log,
        k8s_list_pods,
        k8s_describe_workload,
        k8s_get_events,
        k8s_get_logs,
        k8s_rollout_status,
        docker_container_stats,
        probe_http,
        resolve_dns,
        inspect_tls_certificate,
        redis_info,
        redis_slowlog,
        mysql_read_query,
        web_search,
    ]


class FakeMCPClient:
    """离线替代 MCP 客户端：get_tools() 返回桩工具列表。"""

    def __init__(self, tools: list) -> None:
        self._tools = tools

    async def get_tools(self) -> list:
        return self._tools


@tool("retrieve_knowledge")
async def fake_retrieve_knowledge(query: str) -> str:  # noqa: ARG001
    """检索内部运维知识库（评测桩：始终返回空）。"""
    return ""


DRY_RUN_PLAN = ["查询监控指标与活跃告警", "查询相关日志与事件", "生成诊断报告"]

DRY_RUN_REPORT = """# 诊断报告（dry-run）

## 根因
CPU 使用率过高，磁盘使用率 95%，存在内存泄漏迹象；K8s Pod 处于 CrashLoopBackOff，
DNS 解析失败，证书临近过期，连接池耗尽，队列积压，集群可用性下降。

## 处理建议
扩容并清理磁盘，重启异常容器，轮换证书，优化慢查询与消费速度。"""


class DryRunLLM:
    """无 key 冒烟用的确定性假 LLM：覆盖 planner/executor/replanner 三种调用形态。"""

    def __init__(self) -> None:
        self._round = 0

    def with_structured_output(self, schema: Any, **_kwargs: Any) -> RunnableLambda:
        from app.agent.aiops.planner import Plan
        from app.agent.aiops.replanner import Act, Response

        if schema is Plan:
            return RunnableLambda(lambda _input: Plan(steps=list(DRY_RUN_PLAN)))
        if schema is Act:
            return RunnableLambda(lambda _input: Act(action="continue"))
        if schema is Response:
            return RunnableLambda(lambda _input: Response(response=DRY_RUN_REPORT))
        raise ValueError(f"dry-run LLM 不支持的结构化输出: {schema}")

    def bind_tools(self, tools: list) -> DryRunLLM:  # noqa: ARG002
        return self

    async def ainvoke(self, messages: Any) -> AIMessage:  # noqa: ARG002
        self._round += 1
        if self._round == 1:
            # 第一轮固定调用一次桩工具，验证工具链路
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "prom_active_alerts",
                        "args": {},
                        "id": "dry-run-call-1",
                        "type": "tool_call",
                    }
                ],
            )
        return AIMessage(content="dry-run 步骤结果：已采集监控指标、日志与事件证据。")


def install_stubs(tools: list, dry_run: bool = False) -> None:
    """向三个节点模块注入桩 MCP 客户端与离线 retrieve_knowledge（进程内生效）。"""
    mcp_client = FakeMCPClient(tools)

    async def _fake_mcp_client() -> FakeMCPClient:
        return mcp_client

    for module in NODE_MODULES:
        module.get_mcp_client_with_retry = _fake_mcp_client
        module.retrieve_knowledge = fake_retrieve_knowledge
        if dry_run:
            module.llm_factory.create_chat_model = lambda **kwargs: DryRunLLM()


def load_cases(path: Path) -> list[dict]:
    with path.open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


async def run_case(
    graph: Any,
    case: dict,
    recorder: ToolRecorder,
    timeout: float,
) -> dict:
    """离线执行单条诊断 case 并采集指标。"""
    initial_state = {
        "input": case["question"],
        "plan": [],
        "past_steps": [],
        "degraded": False,
        "response": "",
    }
    config_dict = {
        "configurable": {"thread_id": uuid.uuid4().hex},
        "recursion_limit": config.aiops_recursion_limit,
    }

    llm_before = llm_metrics.snapshot()
    tools_before = recorder.snapshot()
    started = time.perf_counter()

    initial_plan_steps = 0
    executed_steps = 0
    replan_count = 0
    degraded = False
    error: str | None = None
    values: dict = {}

    async def _consume() -> None:
        nonlocal initial_plan_steps, executed_steps, replan_count, degraded
        async for event in graph.astream(
            input=initial_state, config=config_dict, stream_mode="updates"
        ):
            for node_name, output in event.items():
                if not isinstance(output, dict):
                    continue
                if node_name == NODE_PLANNER:
                    initial_plan_steps = len(output.get("plan", []))
                    degraded = bool(output.get("degraded", False))
                elif node_name == NODE_EXECUTOR:
                    # P0.2 起 Executor 按 DAG 批量执行，一次节点调用可能完成多个步骤
                    executed_steps += len(output.get("past_steps", []))
                elif node_name == NODE_REPLANNER and output.get("plan"):
                    # replanner 返回 plan 说明触发了 replan（替换剩余步骤）
                    replan_count += 1

    try:
        await asyncio.wait_for(_consume(), timeout=timeout)
        final_state = await graph.aget_state(config_dict)
        values = dict(final_state.values) if final_state and final_state.values else {}
    except Exception as exc:  # 超时与图执行异常统一记录为失败 case
        error = str(exc) or type(exc).__name__

    latency_ms = (time.perf_counter() - started) * 1000
    llm_delta = diff_snapshots(llm_before, llm_metrics.snapshot())
    called_tools = sorted(set(recorder.delta(tools_before)))
    tool_call_count = len(recorder.delta(tools_before))

    response = str(values.get("response") or "")
    degraded = degraded or bool(values.get("degraded", False))
    expected_tools = set(case.get("expected_tools", []))
    required_keywords = case.get("required_keywords", [])
    keyword_hits = [kw for kw in required_keywords if kw.lower() in response.lower()]
    keyword_coverage = len(keyword_hits) / len(required_keywords) if required_keywords else 1.0
    tool_selection_ok = expected_tools.issubset(called_tools)
    response_ok = error is None and bool(response)
    success = response_ok and tool_selection_ok and keyword_coverage >= 0.5

    tool_details: dict[str, dict] = {}
    for name, count in Counter(recorder.delta(tools_before)).items():
        tool_details[name] = {"calls": count}

    return {
        "case_id": case["case_id"],
        "category": case.get("category"),
        "success": success,
        "error": error,
        "response_ok": response_ok,
        "tool_selection_ok": tool_selection_ok,
        "called_tools": called_tools,
        "expected_tools": sorted(expected_tools),
        "tool_details": tool_details,
        "tool_calls": tool_call_count,
        "keyword_coverage": round(keyword_coverage, 4),
        "matched_keywords": keyword_hits,
        "initial_plan_steps": initial_plan_steps,
        "executed_steps": executed_steps,
        "max_steps": case.get("max_steps"),
        "steps_within_budget": case.get("max_steps") is None
        or executed_steps <= case["max_steps"],
        "replan_count": replan_count,
        "degraded": degraded,
        "latency_ms": round(latency_ms, 2),
        "llm_calls": llm_delta["calls"],
        "input_tokens": llm_delta["input_tokens"],
        "output_tokens": llm_delta["output_tokens"],
        "cost_usd": llm_delta["cost_usd"],
        "response_preview": response[:300],
    }


def summarize(results: list[dict], *, dry_run: bool, dataset_path: Path) -> dict:
    """汇总全部 case 的聚合指标。"""
    total = len(results)
    successes = sum(bool(item["success"]) for item in results)
    plan_steps = [item["initial_plan_steps"] for item in results]
    executed = [item["executed_steps"] for item in results]
    latencies = [item["latency_ms"] for item in results if item["latency_ms"] > 0]

    categories: dict[str, list[dict]] = {}
    for item in results:
        categories.setdefault(str(item.get("category")), []).append(item)
    category_summaries = [
        {
            "category": name,
            "cases": len(items),
            "successes": sum(bool(item["success"]) for item in items),
            "success_rate": round(
                sum(bool(item["success"]) for item in items) / len(items), 4
            ),
        }
        for name, items in sorted(categories.items())
    ]

    total_cost = round(sum(item["cost_usd"] for item in results), 6)
    return {
        "generated_at": datetime.now().astimezone().isoformat(),
        "dataset": str(dataset_path),
        "dry_run": dry_run,
        "rag_model": config.rag_model,
        "reasoning_model": config.llm_reasoning_model,
        "case_count": total,
        "case_successes": successes,
        "case_success_rate": round(successes / total, 4) if total else 0.0,
        "category_summaries": category_summaries,
        # 规划质量
        "average_initial_plan_steps": round(statistics.mean(plan_steps), 2)
        if plan_steps
        else 0.0,
        "plan_steps_distribution": dict(
            sorted(Counter(plan_steps).items(), key=lambda pair: pair[0])
        ),
        "first_plan_hit_rate": round(
            sum(item["replan_count"] == 0 for item in results) / total, 4
        )
        if total
        else 0.0,
        "replan_rate": round(sum(item["replan_count"] > 0 for item in results) / total, 4)
        if total
        else 0.0,
        "average_executed_steps": round(statistics.mean(executed), 2) if executed else 0.0,
        "steps_within_budget_rate": round(
            sum(bool(item["steps_within_budget"]) for item in results) / total, 4
        )
        if total
        else 0.0,
        # 降级
        "degraded_rate": round(sum(bool(item["degraded"]) for item in results) / total, 4)
        if total
        else 0.0,
        # 工具调用
        "average_tool_calls": round(
            sum(item["tool_calls"] for item in results) / total, 2
        )
        if total
        else 0.0,
        "tool_selection": tool_selection_metrics(results),
        # 延迟
        "average_latency_ms": round(statistics.mean(latencies), 2) if latencies else 0.0,
        "p50_latency_ms": percentile(latencies, 0.50),
        "p95_latency_ms": percentile(latencies, 0.95),
        "p99_latency_ms": percentile(latencies, 0.99),
        # 成本
        "total_llm_calls": sum(item["llm_calls"] for item in results),
        "average_input_tokens": round(
            sum(item["input_tokens"] for item in results) / total, 1
        )
        if total
        else 0.0,
        "average_output_tokens": round(
            sum(item["output_tokens"] for item in results) / total, 1
        )
        if total
        else 0.0,
        "total_cost_usd": total_cost,
        "average_cost_usd": round(total_cost / total, 6) if total else 0.0,
        "results": results,
    }


def markdown_report(report: dict) -> str:
    mode = "dry-run（fake LLM + 桩工具，仅链路冒烟）" if report["dry_run"] else "真实 LLM + 桩工具"
    lines = [
        "# AIOps Diagnosis Evaluation (Plan-Execute-Replan)",
        "",
        f"- Generated: {report['generated_at']}",
        f"- Dataset: {report['dataset']}",
        f"- Mode: {mode}",
        f"- Models: {report['rag_model']}（规划/执行）/ {report['reasoning_model']}（最终报告）",
        f"- Cases: {report['case_count']}",
        f"- Case success rate: {report['case_success_rate']:.2%}",
        "",
        "## Per-category success rate",
        "",
        "| Category | Cases | Successes | Success rate |",
        "|---|---:|---:|---:|",
        *[
            f"| {item['category']} | {item['cases']} | {item['successes']} | "
            f"{item['success_rate']:.2%} |"
            for item in report["category_summaries"]
        ],
        "",
        "## Planning quality",
        "",
        f"- Average initial plan steps: {report['average_initial_plan_steps']}",
        f"- Plan steps distribution: "
        f"{json.dumps(report['plan_steps_distribution'], ensure_ascii=False)}",
        f"- First-plan hit rate (no replan): {report['first_plan_hit_rate']:.2%}",
        f"- Replan rate: {report['replan_rate']:.2%}",
        f"- Average executed steps: {report['average_executed_steps']}",
        f"- Steps within budget rate: {report['steps_within_budget_rate']:.2%}",
        f"- Degraded plan rate: {report['degraded_rate']:.2%}",
        "",
        "## Tool calls",
        "",
        f"- Average tool calls per case: {report['average_tool_calls']}",
        f"- Tool selection precision / recall: "
        f"{report['tool_selection']['precision']:.2%} / "
        f"{report['tool_selection']['recall']:.2%}",
        f"- Unnecessary tool calls: {report['tool_selection']['unnecessary_tool_calls']}",
        "",
        "## Latency",
        "",
        f"- Average latency: {report['average_latency_ms'] / 1000:.2f}s",
        f"- P50 / P95 / P99 latency: {report['p50_latency_ms'] / 1000:.2f}s / "
        f"{report['p95_latency_ms'] / 1000:.2f}s / {report['p99_latency_ms'] / 1000:.2f}s",
        "",
        "## Cost",
        "",
        f"- Total LLM calls: {report['total_llm_calls']}",
        f"- Average input / output tokens per case: "
        f"{report['average_input_tokens']} / {report['average_output_tokens']}",
        f"- Total / average cost (USD): "
        f"${report['total_cost_usd']:.6f} / ${report['average_cost_usd']:.6f}",
        "",
        "## Per-case results",
        "",
        "| Case | Success | Steps | Replans | Degraded | Keyword coverage | Latency | Error |",
        "|---|---:|---:|---:|---:|---:|---:|---|",
        *[
            f"| {item['case_id']} | {item['success']} | {item['executed_steps']} | "
            f"{item['replan_count']} | {item['degraded']} | "
            f"{item['keyword_coverage']:.2%} | {item['latency_ms'] / 1000:.2f}s | "
            f"{item['error'] or ''} |"
            for item in report["results"]
        ],
        "",
        "> 数据集由规则种子生成（label_origin=rule_seeded），所有标签 review_status 为 "
        "pending，未经人工复核，不能称为人工标注数据。工具层为确定性桩，工具选择指标反映"
        "的是“给定桩数据下 LLM 的选工具倾向”，不代表真实环境排障能力。dry-run 结果仅用于"
        "链路冒烟，不能作为模型质量结论。",
        "",
        "> Use reviewed labels and a representative task sample before copying these numbers to a resume.",
        "",
    ]
    return "\n".join(lines)


async def run(args: argparse.Namespace) -> dict:
    cases = load_cases(args.dataset)
    if args.sample:
        cases = cases[: args.sample]

    recorder = ToolRecorder()
    install_stubs(build_stub_tools(recorder), dry_run=args.dry_run)
    service = AIOpsService()

    results: list[dict] = []
    for index, case in enumerate(cases, 1):
        try:
            result = await run_case(service.graph, case, recorder, args.timeout)
        except Exception as exc:
            result = {
                "case_id": case["case_id"],
                "category": case.get("category"),
                "success": False,
                "error": str(exc),
                "expected_tools": sorted(case.get("expected_tools", [])),
                "called_tools": [],
                "tool_details": {},
                "tool_calls": 0,
                "keyword_coverage": 0.0,
                "initial_plan_steps": 0,
                "executed_steps": 0,
                "steps_within_budget": False,
                "replan_count": 0,
                "degraded": False,
                "latency_ms": 0.0,
                "llm_calls": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "cost_usd": 0.0,
            }
        results.append(result)
        print(
            f"case={index}/{len(cases)} id={case['case_id']} "
            f"success={result['success']} steps={result['executed_steps']} "
            f"latency_ms={result['latency_ms']}",
            flush=True,
        )
    return summarize(results, dry_run=args.dry_run, dataset_path=args.dataset)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--sample", type=int)
    parser.add_argument("--timeout", type=float, default=600.0, help="单条 case 的超时秒数")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="使用 fake LLM 做无 key 冒烟（不调用真实模型）",
    )
    args = parser.parse_args()
    report = asyncio.run(run(args))
    REPORTS.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    json_path = REPORTS / f"aiops_eval_{stamp}.json"
    md_path = REPORTS / f"aiops_eval_{stamp}.md"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(markdown_report(report), encoding="utf-8")
    print(json_path)
    print(md_path)


if __name__ == "__main__":
    main()
