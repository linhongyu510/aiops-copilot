"""Generate the 400-row corpus-aware RAG v2 query/qrels dataset."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "aiops-docs"
MANIFEST = DOCS / "CORPUS_MANIFEST.json"
OUTPUT = ROOT / "evaluation" / "datasets" / "rag_v2_queries.jsonl"

QUERY_TEMPLATES = (
    ("symptom", "{topic}通常有哪些识别信号？", "easy"),
    ("triage", "遇到{topic}时第一轮应该按什么顺序排查？", "medium"),
    ("evidence", "判断{topic}需要收集哪些关键证据？", "medium"),
    ("root_cause", "{topic}可能由哪些原因造成，如何区分？", "medium"),
    ("mitigation", "{topic}正在影响线上服务时如何先止损？", "hard"),
    ("recovery", "{topic}处理后用什么标准确认已经恢复？", "medium"),
    ("escalation", "什么情况下{topic}必须升级给其他团队？", "easy"),
    ("safety", "处理{topic}时有哪些高风险操作不能直接做？", "hard"),
    ("differential", "如何避免对{topic}只看单一指标就误判？", "hard"),
    ("runbook", "请给出{topic}的证据、排查、止损和升级闭环。", "hard"),
)

OUT_OF_SCOPE_TOPICS = (
    "公司员工年假审批制度",
    "财务报销发票抬头",
    "食堂本周菜单",
    "办公楼停车位申请",
    "产品市场定价策略",
    "客户合同续签流程",
    "招聘面试评分标准",
    "差旅酒店预订政策",
    "个人所得税专项扣除",
    "品牌宣传物料规范",
)


def _sections(content: str) -> tuple[list[str], list[str]]:
    headings: list[str] = []
    facts: list[str] = []
    current_heading = ""
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith("## "):
            current_heading = stripped[3:].strip()
            headings.append(current_heading)
        elif current_heading and stripped and not stripped.startswith(("#", "-", "1.", "2.", "3.")):
            facts.append(re.sub(r"\s+", " ", stripped))
            current_heading = ""
    return headings, facts


def _split_assignments(documents: list[dict]) -> dict[str, str]:
    ordered = sorted(
        documents,
        key=lambda item: hashlib.sha256(item["doc_id"].encode()).hexdigest(),
    )
    pattern = ("train",) * 6 + ("dev",) * 2 + ("test",) * 2
    return {
        item["doc_id"]: pattern[index % len(pattern)]
        for index, item in enumerate(ordered)
    }


def build_dataset() -> list[dict]:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    documents = manifest["documents"]
    assignments = _split_assignments(documents)
    rows: list[dict] = []
    for document in documents:
        content = (DOCS / document["file"]).read_text(encoding="utf-8")
        headings, facts = _sections(content)
        topic = re.sub(r"(处理方案|告警处理方案)$", "", document["title"]).strip()
        for index, (query_type, template, difficulty) in enumerate(QUERY_TEMPLATES, start=1):
            rows.append(
                {
                    "id": f"rag-v2-{document['doc_id']}-{index:02d}",
                    "query": template.format(topic=topic),
                    "category": document["category"],
                    "difficulty": difficulty,
                    "query_type": query_type,
                    "expected_doc_ids": [document["doc_id"]],
                    "expected_sources": [document["file"]],
                    "expected_section_ids": headings,
                    "expected_evidence_spans": facts[:4],
                    "key_facts": facts[:4],
                    "split": assignments[document["doc_id"]],
                    "label_origin": "rule_seeded",
                    "review_status": "pending",
                    "corpus_id": manifest["corpus_id"],
                    "corpus_hash": manifest["corpus_hash"],
                }
            )
    negative_pattern = ("train",) * 6 + ("dev",) * 2 + ("test",) * 2
    negative_queries = [
        f"{prefix}{topic}？"
        for topic in OUT_OF_SCOPE_TOPICS
        for prefix in ("请说明", "内部知识库是否记录", "给出完整的", "如何查询")
    ]
    for index, query in enumerate(negative_queries):
        rows.append(
            {
                "id": f"rag-v2-negative-{index + 1:03d}",
                "query": query,
                "category": "out_of_scope",
                "difficulty": "hard",
                "query_type": "no_answer",
                "expected_doc_ids": [],
                "expected_sources": [],
                "expected_section_ids": [],
                "expected_evidence_spans": [],
                "key_facts": [],
                "split": negative_pattern[index % len(negative_pattern)],
                "label_origin": "rule_seeded",
                "review_status": "pending",
                "corpus_id": manifest["corpus_id"],
                "corpus_hash": manifest["corpus_hash"],
            }
        )
    return rows


def main() -> None:
    rows = build_dataset()
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )
    print(f"{OUTPUT}: {len(rows)} rows")


if __name__ == "__main__":
    main()
