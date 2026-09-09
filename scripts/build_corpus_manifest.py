"""Build a reproducible, provenance-aware manifest for the Runbook corpus."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "aiops-docs"
OUTPUT = DOCS / "CORPUS_MANIFEST.json"

# 目录里除 40 篇 runbook 外的辅助文件（如 MkDocs 首页、AI-Native quickstart 跳转索引）不进 RAG 语料。
# 显式列白名单更安全：新增 runbook 只需保证文件名不在此集合内。
NON_CORPUS_FILES = frozenset({"index.md", "ai_native_quickstart.md"})

CATEGORY_PREFIXES = {
    "kubernetes_": "kubernetes",
    "mysql_": "database",
    "database_": "database",
    "redis_": "cache",
    "kafka_": "messaging",
    "rabbitmq_": "messaging",
    "message_queue_": "messaging",
    "elasticsearch_": "search",
    "linux_": "os",
    "inode_": "os",
    "file_descriptor_": "os",
    "dns_": "network",
    "port_": "network",
    "packet_": "network",
    "certificate_": "network",
}


def category_for(stem: str) -> str:
    for prefix, category in CATEGORY_PREFIXES.items():
        if stem.startswith(prefix):
            return category
    return "application"


def build_manifest() -> dict:
    documents = []
    for path in sorted(DOCS.glob("*.md")):
        # 下划线开头 或 显式列入 NON_CORPUS_FILES 的都视为 wiki 辅助文件，不进 RAG 语料
        if path.name.startswith("_") or path.name in NON_CORPUS_FILES:
            continue
        content = path.read_text(encoding="utf-8")
        title = next(
            (line[2:].strip() for line in content.splitlines() if line.startswith("# ")),
            path.stem,
        )
        documents.append(
            {
                "doc_id": path.stem,
                "file": path.name,
                "title": title,
                "category": category_for(path.stem),
                "version": "1.0",
                "source_urls": [],
                "license": "project-original",
                "review_status": "pending",
                "content_hash": hashlib.sha256(content.encode("utf-8")).hexdigest(),
            }
        )
    corpus_hash = hashlib.sha256(
        "\n".join(item["content_hash"] for item in documents).encode("utf-8")
    ).hexdigest()
    return {
        "corpus_id": "aiops-runbook-v3",
        "manifest_generated_at": datetime.now(UTC).isoformat(),
        "language": "zh-CN",
        "document_count": len(documents),
        "document_types": ["runbook"],
        "corpus_hash": corpus_hash,
        "storage": "Milvus",
        "embedding": {
            "default_model": "BAAI/bge-large-zh-v1.5",
            "revision": "79e7739b6ab944e86d6171e44d24c997fc1e0116",
            "dimensions": 1024,
            "normalized": True,
            "metric": "COSINE",
        },
        "chunking": {
            "strategy": "markdown-header",
            "length_unit": "token",
            "chunk_size": 400,
            "overlap": 64,
            "stable_chunk_ids": True,
        },
        "retrieval": {
            "branches": 8,
            "branch_top_k": 20,
            "rrf_k": 60,
            "rrf_top_k": 30,
            "reranker": "BAAI/bge-reranker-v2-m3",
            "reranker_revision": "953dc6f6f85a1b2dbfca4c34a2796e7dde08d41e",
            "runtime_top_k": 5,
        },
        "evidence_boundary": {
            "label_origin": "rule_seeded",
            "review_status": "pending",
            "human_annotated": False,
            "resume_claims_require_approved_labels": True,
        },
        "documents": documents,
    }


def main() -> None:
    manifest = build_manifest()
    OUTPUT.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"{OUTPUT}: {manifest['document_count']} documents")


if __name__ == "__main__":
    main()
