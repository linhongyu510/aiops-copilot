"""Generate deterministic evaluation datasets from reviewed seed labels.

The generated RAG labels are intentionally marked ``pending``. They become an
"artificially reviewed/annotated" dataset only after a human verifies them.
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATASET_DIR = ROOT / "evaluation" / "datasets"

SEEDS = [
    {
        "source": "cpu_high_usage.md",
        "issue": "CPU 使用率持续过高",
        "aliases": ["CPU 飙高", "处理器负载过高", "HighCPUUsage"],
        "keywords": ["CPU", "进程", "负载"],
        "category": "cpu",
    },
    {
        "source": "memory_high_usage.md",
        "issue": "内存使用率持续过高",
        "aliases": ["内存告警", "内存泄漏", "HighMemoryUsage"],
        "keywords": ["内存", "GC", "OOM"],
        "category": "memory",
    },
    {
        "source": "disk_high_usage.md",
        "issue": "磁盘使用率持续过高",
        "aliases": ["磁盘空间不足", "磁盘告警", "HighDiskUsage"],
        "keywords": ["磁盘", "空间", "文件"],
        "category": "disk",
    },
    {
        "source": "service_unavailable.md",
        "issue": "服务不可用",
        "aliases": ["接口 503", "服务宕机", "ServiceUnavailable"],
        "keywords": ["服务", "日志", "依赖"],
        "category": "availability",
    },
    {
        "source": "slow_response.md",
        "issue": "接口响应缓慢",
        "aliases": ["请求超时", "接口延迟升高", "SlowResponse"],
        "keywords": ["响应", "延迟", "日志"],
        "category": "latency",
    },
]

FRAMES = [
    "{service} 出现{issue}，第一步应该检查什么？",
    "请给出{issue}的标准排查顺序。",
    "收到{issue}告警后需要收集哪些证据？",
    "怎样区分{issue}是应用问题还是基础设施问题？",
    "{issue}常见根因有哪些？",
    "请根据运维手册说明{issue}的临时止损措施。",
    "{issue}恢复后需要做哪些复盘和预防？",
    "排查{issue}时应该查询哪些日志？",
    "排查{issue}时应该关注哪些监控指标？",
    "如何确认{issue}已经恢复？",
    "值班工程师处理{issue}时有哪些注意事项？",
    "请把{issue}的排查过程整理成三步。",
    "遇到{issue}，有哪些操作不能直接在生产环境执行？",
    "{issue}与最近发布可能有什么关系？",
    "如何为{issue}确定影响范围和优先级？",
    "请生成{issue}的 OnCall 检查清单。",
    "如果{issue}反复出现，应如何进一步定位？",
    "{issue}排查中应如何保留关键证据？",
    "请给出{issue}的根因分析框架。",
    "新同学遇到{issue}应该按什么顺序处理？",
]

STYLES = [
    ("正式", "{query}"),
    ("口语", "线上突然{query}"),
    ("简洁", "简要回答：{query}"),
    ("约束", "只基于内部知识库回答：{query}"),
    ("场景", "当前是业务高峰期，{query}"),
    ("复盘", "从事后复盘角度说明：{query}"),
]

SERVICES = ["order-service", "payment-service", "gateway", "user-service", "data-sync-service"]

TOOL_SCENARIOS = [
    ("logs", "search_log", "查询 order-service 最近 15 分钟的错误日志"),
    ("monitoring", "query_cpu_metrics", "查询 order-service 最近 15 分钟的 CPU 监控指标"),
    ("mysql", "mysql_read_query", "只读查询最近 5 条服务告警记录"),
    ("web_search", "web_search", "联网搜索 HTTP 503 的官方排障资料"),
]
EXPECTED_ARGUMENT_SCHEMAS = {
    "retrieve_knowledge": {"query": "str"},
    "search_log": {"topic_id": "str", "start_time": "int", "end_time": "int"},
    "query_cpu_metrics": {"service_name": "str"},
    "mysql_read_query": {"sql": "str"},
    "web_search": {"query": "str"},
}
SCENARIO_SUFFIXES = {
    "success": "，返回正常结果并总结关键证据。",
    "timeout": "。测试夹具将注入超时，请识别超时并给出降级说明。",
    "error": "。测试夹具将注入错误返回，请识别错误并给出降级说明。",
}


def split_for_frame(frame_index: int) -> str:
    """按问题模板分组，避免同一模板的不同改写跨集合泄漏。"""
    if frame_index < 12:
        return "train"
    if frame_index < 16:
        return "dev"
    return "test"


def build_rag_dataset() -> list[dict]:
    rows: list[dict] = []
    for seed_index, seed in enumerate(SEEDS):
        for frame_index, frame in enumerate(FRAMES):
            for style_index, (style, wrapper) in enumerate(STYLES):
                alias = seed["aliases"][(frame_index + style_index) % len(seed["aliases"])]
                service = SERVICES[(seed_index + frame_index) % len(SERVICES)]
                base_query = frame.format(service=service, issue=alias)
                rows.append(
                    {
                        "id": f"rag-{seed_index + 1:02d}-{frame_index + 1:02d}-{style_index + 1:02d}",
                        "query": wrapper.format(query=base_query),
                        "expected_sources": [seed["source"]],
                        "expected_keywords": seed["keywords"],
                        "category": seed["category"],
                        "style": style,
                        "difficulty": "medium" if style_index >= 3 else "easy",
                        "split": split_for_frame(frame_index),
                        "label_origin": "rule_seeded",
                        "review_status": "pending",
                    }
                )
    return rows


def build_agent_dataset() -> list[dict]:
    rows: list[dict] = []
    for seed_index, seed in enumerate(SEEDS):
        for task_index, frame in enumerate(FRAMES[:6]):
            issue = seed["aliases"][task_index % len(seed["aliases"])]
            query = frame.format(service=SERVICES[task_index % len(SERVICES)], issue=issue)
            rows.append(
                {
                    "id": f"agent-{seed_index + 1:02d}-{task_index + 1:02d}",
                    "question": f"请基于内部运维知识库回答：{query}",
                    "expected_tools": ["retrieve_knowledge"],
                    "expected_argument_schemas": {
                        "retrieve_knowledge": EXPECTED_ARGUMENT_SCHEMAS["retrieve_knowledge"]
                    },
                    "expected_keywords": seed["keywords"],
                    "category": seed["category"],
                    "review_status": "pending",
                }
            )
    for tool_index, (category, tool, question) in enumerate(TOOL_SCENARIOS, start=1):
        for scenario_index, scenario in enumerate(SCENARIO_SUFFIXES, start=1):
            rows.append(
                {
                    "id": f"agent-tool-{tool_index:02d}-{scenario_index:02d}",
                    "question": question + SCENARIO_SUFFIXES[scenario],
                    "expected_tools": [tool],
                    "expected_argument_schemas": {
                        tool: EXPECTED_ARGUMENT_SCHEMAS[tool]
                    },
                    "expected_keywords": [],
                    "category": category,
                    "scenario": scenario,
                    "expected_outcome": scenario,
                    "fixture_id": f"{tool}:{scenario}",
                    "label_origin": "synthetic_fault_fixture",
                    "review_status": "fixture",
                }
            )
    return rows


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def main() -> None:
    rag_rows = build_rag_dataset()
    agent_rows = build_agent_dataset()
    write_jsonl(DATASET_DIR / "rag_queries.jsonl", rag_rows)
    write_jsonl(DATASET_DIR / "agent_tasks.jsonl", agent_rows)
    print(f"wrote {len(rag_rows)} RAG rows and {len(agent_rows)} agent rows to {DATASET_DIR}")


if __name__ == "__main__":
    main()
