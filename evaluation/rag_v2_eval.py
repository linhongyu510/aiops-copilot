"""Reproducible RAG v2 retrieval evaluation with qrels and bootstrap intervals."""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import random
import statistics
import time
from collections import Counter
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from langchain_core.documents import Document
from langchain_text_splitters import MarkdownHeaderTextSplitter, RecursiveCharacterTextSplitter

from evaluation.rag_baselines import BM25Index
from evaluation.rag_eval import _tokens, cosine, hashing_embedding

ROOT = Path(__file__).resolve().parents[1]
DATASET = ROOT / "evaluation" / "datasets" / "rag_v2_queries.jsonl"
DOCS = ROOT / "aiops-docs"
MANIFEST = DOCS / "CORPUS_MANIFEST.json"
REPORTS = ROOT / "evaluation" / "reports"


def load_rows(path: Path, split: str) -> list[dict]:
    rows = [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    return rows if split == "all" else [row for row in rows if row["split"] == split]


def runtime_chunks() -> list[Document]:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    chunking = manifest["chunking"]
    header_splitter = MarkdownHeaderTextSplitter(
        headers_to_split_on=[("#", "h1"), ("##", "h2")],
        strip_headers=False,
    )
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=int(chunking["chunk_size"]),
        chunk_overlap=int(chunking["overlap"]),
        length_function=len,
        separators=["\n\n", "\n", "。", "；", "，", " ", ""],
    )
    chunks: list[Document] = []
    for path in sorted(DOCS.glob("*.md")):
        sections = header_splitter.split_text(path.read_text(encoding="utf-8"))
        for chunk in splitter.split_documents(sections):
            chunk.metadata["source"] = path.name
            chunk.metadata["_file_name"] = path.name
            chunks.append(chunk)
    return chunks


def ranked_sources(indices: list[int], chunks: list[Document]) -> list[str]:
    return [
        str(chunks[index].metadata.get("_file_name") or chunks[index].metadata.get("source"))
        for index in indices
    ]


def evaluate(rows: list[dict], rankings: list[list[str]]) -> tuple[dict, list[dict]]:
    recall_at = {5: [], 10: [], 20: []}
    precision_at_5: list[float] = []
    reciprocal_ranks: list[float] = []
    ndcg: list[float] = []
    no_answer: list[float] = []
    errors: list[dict] = []
    by_category: dict[str, list[float]] = {}
    for row, ranking in zip(rows, rankings, strict=True):
        expected = set(row["expected_sources"])
        if not expected:
            correct = float(not ranking)
            no_answer.append(correct)
            if not correct:
                errors.append(
                    {
                        "id": row["id"],
                        "error_type": "false_positive",
                        "retrieved_sources": ranking[:5],
                    }
                )
            continue
        relevance = [1 if source in expected else 0 for source in ranking]
        for k in recall_at:
            recall_at[k].append(
                len(expected & set(ranking[:k])) / len(expected)
            )
        precision_at_5.append(sum(relevance[:5]) / 5)
        relevant_rank = next(
            (rank for rank, value in enumerate(relevance[:10], start=1) if value),
            None,
        )
        reciprocal_ranks.append(1.0 / relevant_rank if relevant_rank else 0.0)
        dcg = sum(value / math.log2(index + 2) for index, value in enumerate(relevance[:10]))
        ideal = sum(
            1.0 / math.log2(index + 2)
            for index in range(min(len(expected), 10))
        )
        ndcg.append(dcg / ideal if ideal else 0.0)
        hit = float(bool(relevant_rank))
        by_category.setdefault(row["category"], []).append(hit)
        if not relevant_rank:
            errors.append(
                {
                    "id": row["id"],
                    "error_type": "retrieval_miss",
                    "expected_sources": sorted(expected),
                    "retrieved_sources": ranking[:10],
                }
            )
    metrics = {
        **{
            f"recall_at_{k}": round(statistics.mean(values), 4) if values else 0.0
            for k, values in recall_at.items()
        },
        "precision_at_5": round(statistics.mean(precision_at_5), 4)
        if precision_at_5
        else 0.0,
        "mrr_at_10": round(statistics.mean(reciprocal_ranks), 4)
        if reciprocal_ranks
        else 0.0,
        "ndcg_at_10": round(statistics.mean(ndcg), 4) if ndcg else 0.0,
        "no_answer_accuracy": round(statistics.mean(no_answer), 4)
        if no_answer
        else None,
        "category_hit_at_10": {
            category: round(statistics.mean(values), 4)
            for category, values in sorted(by_category.items())
        },
        "errors": len(errors),
    }
    return metrics, errors


def bootstrap_ci(
    rows: list[dict],
    rankings: list[list[str]],
    metric: str,
    samples: int = 500,
    seed: int = 20260730,
) -> list[float]:
    randomizer = random.Random(seed)
    values: list[float] = []
    for _ in range(samples):
        indices = [randomizer.randrange(len(rows)) for _ in rows]
        sampled_rows = [rows[index] for index in indices]
        sampled_rankings = [rankings[index] for index in indices]
        metrics, _ = evaluate(sampled_rows, sampled_rankings)
        value = metrics.get(metric)
        if isinstance(value, (int, float)):
            values.append(float(value))
    values.sort()
    if not values:
        return [0.0, 0.0]
    return [
        round(values[int(0.025 * (len(values) - 1))], 4),
        round(values[int(0.975 * (len(values) - 1))], 4),
    ]


def run_bm25(rows: list[dict], chunks: list[Document]) -> list[list[str]]:
    index = BM25Index([chunk.page_content for chunk in chunks])
    rankings: list[list[str]] = []
    for row in rows:
        indices = index.search(row["query"], 20)
        # BM25Index returns arbitrary zero-score rows for out-of-domain queries.
        if not any(token in index.idf for token in _tokens(row["query"])):
            indices = []
        rankings.append(ranked_sources(indices, chunks))
    return rankings


def run_vector(
    rows: list[dict],
    chunks: list[Document],
    embed_documents: Callable[[list[str]], list[list[float]]],
    embed_queries: Callable[[list[str]], list[list[float]]],
) -> list[list[str]]:
    document_vectors = embed_documents([chunk.page_content for chunk in chunks])
    query_vectors = embed_queries([row["query"] for row in rows])
    rankings: list[list[str]] = []
    for vector in query_vectors:
        indices = sorted(
            range(len(chunks)),
            key=lambda index: cosine(vector, document_vectors[index]),
            reverse=True,
        )[:20]
        rankings.append(ranked_sources(indices, chunks))
    return rankings


async def run_full(rows: list[dict]) -> tuple[list[list[str]], list[float]]:
    from app.core.milvus_client import milvus_manager
    from app.services.hybrid_retrieval_service import hybrid_retrieval_service

    milvus_manager.connect()
    rankings: list[list[str]] = []
    latencies: list[float] = []
    try:
        for row in rows:
            started = time.perf_counter()
            candidates, _ = await hybrid_retrieval_service.search(row["query"], 20)
            latencies.append((time.perf_counter() - started) * 1000)
            rankings.append(
                [
                    str(
                        item.metadata.get("_file_name")
                        or item.metadata.get("source")
                        or item.metadata.get("_source")
                    )
                    for item in candidates
                ]
            )
    finally:
        milvus_manager.close()
    return rankings, latencies


def percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * fraction) - 1)]


def validate_runtime_contract(manifest: dict) -> None:
    from app.config import config

    expected_model = manifest["embedding"]["default_model"]
    expected_revision = manifest["embedding"]["revision"]
    expected_dimensions = int(manifest["embedding"]["dimensions"])
    mismatches = []
    if config.local_embedding_model != expected_model:
        mismatches.append(
            f"LOCAL_EMBEDDING_MODEL={config.local_embedding_model!r}, expected={expected_model!r}"
        )
    if config.embedding_dimensions != expected_dimensions:
        mismatches.append(
            f"EMBEDDING_DIMENSIONS={config.embedding_dimensions}, expected={expected_dimensions}"
        )
    if config.local_embedding_revision != expected_revision:
        mismatches.append(
            "LOCAL_EMBEDDING_REVISION="
            f"{config.local_embedding_revision!r}, expected={expected_revision!r}"
        )
    if mismatches:
        raise SystemExit(
            "RAG v2 模型配置不匹配；为防止生成不可引用的报告已停止："
            + "; ".join(mismatches)
        )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", type=Path, default=DATASET)
    parser.add_argument("--split", choices=["train", "dev", "test", "all"], default="dev")
    parser.add_argument(
        "--backend",
        choices=["bm25", "hashing", "local", "full"],
        default="hashing",
    )
    parser.add_argument("--sample", type=int)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=REPORTS,
        help="Report directory (defaults to evaluation/reports).",
    )
    args = parser.parse_args()

    rows = load_rows(args.dataset, args.split)
    if args.sample:
        rows = rows[: args.sample]
    chunks = runtime_chunks()
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    if args.backend in {"local", "full"}:
        validate_runtime_contract(manifest)
    started = time.perf_counter()
    latencies: list[float] = []
    if args.backend == "bm25":
        rankings = run_bm25(rows, chunks)
    elif args.backend == "hashing":
        rankings = run_vector(
            rows,
            chunks,
            lambda values: [hashing_embedding(value) for value in values],
            lambda values: [hashing_embedding(value) for value in values],
        )
    elif args.backend == "local":
        from evaluation.rag_eval import model_embedders

        embed_documents, embed_queries = model_embedders("local")
        rankings = run_vector(rows, chunks, embed_documents, embed_queries)
    else:
        rankings, latencies = asyncio.run(run_full(rows))
    elapsed = (time.perf_counter() - started) * 1000
    metrics, errors = evaluate(rows, rankings)
    report = {
        "generated_at": datetime.now().astimezone().isoformat(),
        "backend": args.backend,
        "dataset": str(args.dataset),
        "dataset_split": args.split,
        "dataset_size": len(rows),
        "review_status": dict(Counter(row["review_status"] for row in rows)),
        "corpus_id": manifest["corpus_id"],
        "corpus_hash": manifest["corpus_hash"],
        "model": manifest["embedding"]["default_model"],
        "model_revision": manifest["embedding"]["revision"],
        "random_seed": 20260730,
        "metrics": metrics,
        "bootstrap_95_ci": {
            metric: bootstrap_ci(rows, rankings, metric)
            for metric in ("recall_at_20", "mrr_at_10", "ndcg_at_10")
        },
        "latency_ms": {
            "total": round(elapsed, 3),
            "p50": round(percentile(latencies, 0.5), 3),
            "p95": round(percentile(latencies, 0.95), 3),
        },
        "errors": errors,
        "evidence_warning": (
            "All current v2 labels are rule-seeded/pending and must not be described "
            "as human-annotated until review_status is approved."
        ),
    }
    args.output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    output = args.output_dir / f"rag_v2_{args.backend}_{args.split}_{stamp}.json"
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(output)


if __name__ == "__main__":
    main()
