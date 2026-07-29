"""同一固定测试集上的 BM25、初始 BGE 与优化 BGE 对照实验。"""

from __future__ import annotations

import argparse
import json
import math
import statistics
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

from langchain_core.documents import Document

from evaluation.rag_eval import (
    DEFAULT_DATASET,
    DEFAULT_DOCS,
    REPORTS,
    _tokens,
    cosine,
    load_jsonl,
    model_embedders,
    percentile,
    split_corpus,
    split_plain_corpus,
)


class BM25Index:
    """无外部依赖的 Okapi BM25 基线。"""

    def __init__(self, documents: list[str], k1: float = 1.5, b: float = 0.75) -> None:
        self.k1 = k1
        self.b = b
        self.tokens = [_tokens(document) for document in documents]
        self.lengths = [len(tokens) for tokens in self.tokens]
        self.average_length = statistics.mean(self.lengths) if self.lengths else 0.0
        self.term_frequencies = [Counter(tokens) for tokens in self.tokens]
        document_frequencies: Counter[str] = Counter()
        for tokens in self.tokens:
            document_frequencies.update(set(tokens))
        count = len(self.tokens)
        self.idf = {
            token: math.log(1.0 + (count - frequency + 0.5) / (frequency + 0.5))
            for token, frequency in document_frequencies.items()
        }

    def search(self, query: str, limit: int) -> list[int]:
        scores: list[float] = []
        average_length = self.average_length or 1.0
        for frequencies, length in zip(self.term_frequencies, self.lengths, strict=True):
            score = 0.0
            for token in _tokens(query):
                frequency = frequencies.get(token, 0)
                if not frequency:
                    continue
                denominator = frequency + self.k1 * (
                    1.0 - self.b + self.b * length / average_length
                )
                score += self.idf.get(token, 0.0) * (frequency * (self.k1 + 1.0) / denominator)
            scores.append(score)
        return sorted(range(len(scores)), key=scores.__getitem__, reverse=True)[:limit]


def evaluate_ranker(rows: list[dict], chunks: list[Document], search) -> dict:
    top_k = 5
    hits = 0
    reciprocal_ranks: list[float] = []
    latencies: list[float] = []
    errors: list[dict] = []
    category_totals: Counter[str] = Counter()
    category_hits: Counter[str] = Counter()
    for index, row in enumerate(rows):
        started = time.perf_counter()
        ranked = search(index, row["query"], 10)
        latencies.append((time.perf_counter() - started) * 1000)
        expected = set(row["expected_sources"])
        category = row.get("category", "unknown")
        category_totals[category] += 1
        relevant_rank = next(
            (
                rank
                for rank, chunk_index in enumerate(ranked, start=1)
                if chunks[chunk_index].metadata.get("source") in expected
            ),
            None,
        )
        if relevant_rank is not None and relevant_rank <= top_k:
            hits += 1
            category_hits[category] += 1
            reciprocal_ranks.append(1.0 / relevant_rank)
        else:
            reciprocal_ranks.append(0.0)
            errors.append(
                {
                    "id": row["id"],
                    "category": category,
                    "error_type": "ranking_error" if relevant_rank is not None else "semantic_miss",
                    "best_relevant_rank": relevant_rank,
                }
            )
    total = len(rows)
    return {
        "queries": total,
        "hit_at_5": round(hits / total, 4) if total else 0.0,
        "mrr_at_5": round(statistics.mean(reciprocal_ranks), 4) if reciprocal_ranks else 0.0,
        "p50_retrieval_latency_ms": percentile(latencies, 0.50),
        "p95_retrieval_latency_ms": percentile(latencies, 0.95),
        "failures": len(errors),
        "error_attribution": dict(Counter(item["error_type"] for item in errors)),
        "category_hit_rate": {
            category: round(category_hits[category] / count, 4)
            for category, count in sorted(category_totals.items())
        },
        "errors": errors,
    }


def diversify_ranked_sources(ranked: list[int], chunks: list[Document], limit: int) -> list[int]:
    """Prefer distinct source documents while retaining original similarity order."""
    selected: list[int] = []
    deferred: list[int] = []
    seen_sources: set[str] = set()
    for chunk_index in ranked:
        source = str(chunks[chunk_index].metadata.get("source", ""))
        if source and source not in seen_sources:
            selected.append(chunk_index)
            seen_sources.add(source)
        else:
            deferred.append(chunk_index)
        if len(selected) >= limit:
            return selected
    return (selected + deferred)[:limit]


def run_bm25(rows: list[dict], chunks: list[Document]) -> dict:
    index = BM25Index([chunk.page_content for chunk in chunks])
    return evaluate_ranker(rows, chunks, lambda _i, query, limit: index.search(query, limit))


def run_bge(
    rows: list[dict],
    chunks: list[Document],
    query_vectors: list[list[float]],
    embed_documents,
    diversify_sources: bool = False,
) -> dict:
    chunk_vectors = embed_documents([chunk.page_content for chunk in chunks])

    def search(index: int, _query: str, limit: int) -> list[int]:
        query_vector = query_vectors[index]
        ranked = sorted(
            range(len(chunks)),
            key=lambda chunk_index: cosine(query_vector, chunk_vectors[chunk_index]),
            reverse=True,
        )
        if diversify_sources:
            return diversify_ranked_sources(ranked, chunks, limit)
        return ranked[:limit]

    return evaluate_ranker(rows, chunks, search)


def markdown_report(report: dict) -> str:
    lines = [
        "# RAG Baseline Comparison",
        "",
        f"- Generated: {report['generated_at']}",
        f"- Dataset: {report['dataset_size']} fixed queries",
        f"- Dataset split: {report['dataset_split']}",
        f"- Label review: {report['label_review']}",
        "- Metric: all experiments use Hit@5 / MRR@5",
        "",
        "| Experiment | Splitter | Chunk | Overlap | Hit@5 | MRR@5 | P95 ms | Failures |",
        "|---|---|---:|---:|---:|---:|---:|---:|",
    ]
    for item in report["experiments"]:
        metrics = item["metrics"]
        lines.append(
            f"| {item['name']} | {item['splitter']} | {item['chunk_size']} | "
            f"{item['overlap']} | {metrics['hit_at_5']:.2%} | "
            f"{metrics['mrr_at_5']:.2%} | {metrics['p95_retrieval_latency_ms']:.3f} | "
            f"{metrics['failures']} |"
        )
    improvement = report["improvement"]
    lines.extend(
        [
            "",
            "## Same-metric improvement",
            "",
            f"Initial BGE Hit@5 {improvement['initial_hit_at_5']:.2%} -> tuned BGE "
            f"{improvement['optimized_hit_at_5']:.2%} "
            f"({improvement['absolute_percentage_points']:+.2f} percentage points).",
            "",
            "> Labels must be human-reviewed before this number is presented as a final resume result.",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--docs", type=Path, default=DEFAULT_DOCS)
    parser.add_argument("--sample", type=int)
    parser.add_argument("--split", choices=["train", "dev", "test", "all"], default="test")
    args = parser.parse_args()

    rows = load_jsonl(args.dataset, args.sample, args.split)
    if not rows:
        raise SystemExit(f"dataset split {args.split!r} has no rows")
    plain_chunks = split_plain_corpus(args.docs, chunk_size=1200, overlap=0)
    initial_chunks = split_corpus(args.docs, chunk_size=800, overlap=100)
    embed_documents, embed_queries = model_embedders("local")
    query_vectors = embed_queries([row["query"] for row in rows])
    experiments = [
        {
            "name": "BM25 baseline",
            "splitter": "plain",
            "chunk_size": 1200,
            "overlap": 0,
            "chunks": len(plain_chunks),
            "metrics": run_bm25(rows, plain_chunks),
        },
        {
            "name": "Initial BGE",
            "splitter": "markdown-header",
            "chunk_size": 800,
            "overlap": 100,
            "chunks": len(initial_chunks),
            "metrics": run_bge(rows, initial_chunks, query_vectors, embed_documents),
        },
        {
            "name": "Tuned BGE",
            "splitter": "plain",
            "chunk_size": 1200,
            "overlap": 0,
            "chunks": len(plain_chunks),
            "metrics": run_bge(
                rows,
                plain_chunks,
                query_vectors,
                embed_documents,
                diversify_sources=True,
            ),
            "source_diversification": True,
        },
    ]
    initial = experiments[1]["metrics"]["hit_at_5"]
    optimized = experiments[2]["metrics"]["hit_at_5"]
    review_counts = Counter(row.get("review_status", "unknown") for row in rows)
    report = {
        "generated_at": datetime.now().astimezone().isoformat(),
        "dataset_size": len(rows),
        "dataset_split": args.split,
        "label_review": dict(review_counts),
        "experiments": experiments,
        "improvement": {
            "initial_hit_at_5": initial,
            "optimized_hit_at_5": optimized,
            "absolute_percentage_points": round((optimized - initial) * 100, 2),
        },
    }
    REPORTS.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    json_path = REPORTS / f"rag_baselines_{stamp}.json"
    md_path = REPORTS / f"rag_baselines_{stamp}.md"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(markdown_report(report), encoding="utf-8")
    print(json_path)
    print(md_path)


if __name__ == "__main__":
    main()
