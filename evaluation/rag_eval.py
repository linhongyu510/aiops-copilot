"""RAG retrieval benchmark with Chunk Size / overlap / Top-K sweeps."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import statistics
import time
from collections import Counter
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from langchain_core.documents import Document
from langchain_text_splitters import MarkdownHeaderTextSplitter, RecursiveCharacterTextSplitter

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATASET = ROOT / "evaluation" / "datasets" / "rag_queries.jsonl"
DEFAULT_DOCS = ROOT / "aiops-docs"
REPORTS = ROOT / "evaluation" / "reports"


def load_jsonl(
    path: Path, sample: int | None = None, dataset_split: str = "all"
) -> list[dict]:
    with path.open(encoding="utf-8") as handle:
        rows = [json.loads(line) for line in handle if line.strip()]
    if dataset_split != "all":
        rows = [row for row in rows if row.get("split") == dataset_split]
    return rows[:sample] if sample else rows


def split_corpus(docs_dir: Path, chunk_size: int, overlap: int) -> list[Document]:
    header_splitter = MarkdownHeaderTextSplitter(
        headers_to_split_on=[("#", "h1"), ("##", "h2")],
        strip_headers=False,
    )
    text_splitter = RecursiveCharacterTextSplitter(
        chunk_size=chunk_size,
        chunk_overlap=overlap,
        length_function=len,
    )
    chunks: list[Document] = []
    for path in sorted(docs_dir.glob("*.md")):
        sections = header_splitter.split_text(path.read_text(encoding="utf-8"))
        for chunk in text_splitter.split_documents(sections):
            chunk.metadata["source"] = path.name
            chunks.append(chunk)
    return chunks


def split_plain_corpus(docs_dir: Path, chunk_size: int, overlap: int) -> list[Document]:
    """不使用 Markdown 标题边界的固定长度切分。"""
    text_splitter = RecursiveCharacterTextSplitter(
        chunk_size=chunk_size,
        chunk_overlap=overlap,
        length_function=len,
    )
    chunks: list[Document] = []
    for path in sorted(docs_dir.glob("*.md")):
        for chunk in text_splitter.create_documents([path.read_text(encoding="utf-8")]):
            chunk.metadata["source"] = path.name
            chunks.append(chunk)
    return chunks


def _tokens(text: str) -> list[str]:
    lowered = text.lower()
    latin = re.findall(r"[a-z0-9_]+", lowered)
    chinese = re.findall(r"[\u4e00-\u9fff]", lowered)
    bigrams = ["".join(chinese[index : index + 2]) for index in range(len(chinese) - 1)]
    return latin + chinese + bigrams


def hashing_embedding(text: str, dimensions: int = 2048) -> list[float]:
    vector = [0.0] * dimensions
    for token in _tokens(text):
        digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
        index = int.from_bytes(digest, "big") % dimensions
        vector[index] += 1.0
    norm = math.sqrt(sum(value * value for value in vector)) or 1.0
    return [value / norm for value in vector]


def model_embedders(
    backend: str,
) -> tuple[
    Callable[[list[str]], list[list[float]]],
    Callable[[list[str]], list[list[float]]],
]:
    from app.config import config
    from app.services.vector_embedding_service import (
        DashScopeEmbeddings,
        LocalSentenceTransformerEmbeddings,
    )

    if backend == "local":
        embedding_service = LocalSentenceTransformerEmbeddings(
            model_name=config.local_embedding_model,
            dimensions=config.embedding_dimensions,
            device=config.local_embedding_device,
            batch_size=config.local_embedding_batch_size,
            cache_dir=config.local_embedding_cache_dir,
            source=config.local_embedding_source,
            modelscope_model_id=config.modelscope_embedding_model,
        )
    else:
        embedding_service = DashScopeEmbeddings(
            api_key=config.dashscope_api_key,
            model=config.dashscope_embedding_model,
            dimensions=config.embedding_dimensions,
        )

    def embed_documents(texts: list[str]) -> list[list[float]]:
        return embedding_service.embed_documents(texts)

    def embed_queries(texts: list[str]) -> list[list[float]]:
        batch_method = getattr(embedding_service, "embed_queries", None)
        if batch_method is not None:
            return batch_method(texts)
        return [embedding_service.embed_query(text) for text in texts]

    return embed_documents, embed_queries


def cosine(left: list[float], right: list[float]) -> float:
    return sum(a * b for a, b in zip(left, right, strict=True))


def percentile(values: list[float], value: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, math.ceil(value * len(ordered)) - 1)
    return round(ordered[index], 3)


def evaluate_config(
    rows: list[dict],
    query_vectors: list[list[float]],
    chunks: list[Document],
    chunk_vectors: list[list[float]],
    top_k: int,
) -> tuple[dict, list[dict]]:
    hits = 0
    reciprocal_ranks: list[float] = []
    latencies: list[float] = []
    errors: list[dict] = []
    category_totals: Counter[str] = Counter()
    category_hits: Counter[str] = Counter()
    search_limit = max(top_k + 5, 10)

    for row, query_vector in zip(rows, query_vectors, strict=True):
        started = time.perf_counter()
        ranked = sorted(
            range(len(chunks)),
            key=lambda index: cosine(query_vector, chunk_vectors[index]),
            reverse=True,
        )[:search_limit]
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
            error_type = "ranking_error" if relevant_rank is not None else "semantic_miss"
            errors.append(
                {
                    "id": row["id"],
                    "query": row["query"],
                    "category": category,
                    "error_type": error_type,
                    "expected_sources": sorted(expected),
                    "best_relevant_rank": relevant_rank,
                    "retrieved_sources": [
                        chunks[index].metadata.get("source") for index in ranked[:top_k]
                    ],
                }
            )

    total = len(rows)
    return (
        {
            "queries": total,
            f"hit_at_{top_k}": round(hits / total, 4) if total else 0.0,
            f"mrr_at_{top_k}": round(statistics.mean(reciprocal_ranks), 4)
            if reciprocal_ranks
            else 0.0,
            "average_retrieval_latency_ms": round(statistics.mean(latencies), 3)
            if latencies
            else 0.0,
            "p50_retrieval_latency_ms": percentile(latencies, 0.50),
            "p95_retrieval_latency_ms": percentile(latencies, 0.95),
            "failures": len(errors),
            "error_attribution": dict(Counter(item["error_type"] for item in errors)),
            "category_hit_rate": {
                category: round(category_hits[category] / count, 4)
                for category, count in sorted(category_totals.items())
            },
        },
        errors,
    )


def markdown_report(report: dict) -> str:
    lines = [
        "# RAG Retrieval Evaluation",
        "",
        f"- Generated: {report['generated_at']}",
        f"- Backend: {report['backend']}",
        f"- Dataset size: {report['dataset_size']}",
        f"- Dataset split: {report['dataset_split']}",
        f"- Label review: {report['label_review']}",
        "",
        "| Splitter | Chunk | Overlap | Top-K | Hit@K | MRR@K | P50 ms | P95 ms | Failures |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for item in report["experiments"]:
        metrics = item["metrics"]
        k = item["top_k"]
        lines.append(
            f"| {item['splitter']} | {item['chunk_size']} | {item['overlap']} | {k} | "
            f"{metrics[f'hit_at_{k}']:.2%} | {metrics[f'mrr_at_{k}']:.2%} | "
            f"{metrics['p50_retrieval_latency_ms']:.3f} | "
            f"{metrics['p95_retrieval_latency_ms']:.3f} | {metrics['failures']} |"
        )
    best = report["best_experiment"]
    lines.extend(
        [
            "",
            "## Best configuration",
            "",
            f"Splitter={best['splitter']}, Chunk Size={best['chunk_size']}, "
            f"Overlap={best['overlap']}, Top-K={best['top_k']}",
            "",
            "> Hashing 仅用于离线冒烟测试；简历中的检索指标必须使用 `--backend local` 或 `--backend dashscope`，并基于已复核标签。",
            "",
        ]
    )
    return "\n".join(lines)


def parse_ints(value: str) -> list[int]:
    return [int(item.strip()) for item in value.split(",") if item.strip()]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--docs", type=Path, default=DEFAULT_DOCS)
    parser.add_argument("--backend", choices=["hashing", "local", "dashscope"], default="hashing")
    parser.add_argument("--splitters", default="markdown-header,plain")
    parser.add_argument("--chunk-sizes", default="400,800,1200")
    parser.add_argument("--overlaps", default="0,50,100")
    parser.add_argument("--top-ks", default="1,3,5")
    parser.add_argument("--sample", type=int)
    parser.add_argument("--split", choices=["train", "dev", "test", "all"], default="dev")
    args = parser.parse_args()

    rows = load_jsonl(args.dataset, args.sample, args.split)
    if not rows:
        raise SystemExit(f"dataset split {args.split!r} has no rows")
    if args.backend == "hashing":
        embed_documents = embed_queries = lambda values: [
            hashing_embedding(value) for value in values
        ]
    else:
        embed_documents, embed_queries = model_embedders(args.backend)
    query_vectors = embed_queries([row["query"] for row in rows])
    experiments: list[dict] = []
    all_errors: dict[str, list[dict]] = {}

    splitter_names = [item.strip() for item in args.splitters.split(",") if item.strip()]
    invalid_splitters = set(splitter_names) - {"markdown-header", "plain"}
    if invalid_splitters:
        raise SystemExit(f"unsupported splitters: {sorted(invalid_splitters)}")
    for splitter_name in splitter_names:
        splitter = split_corpus if splitter_name == "markdown-header" else split_plain_corpus
        for chunk_size in parse_ints(args.chunk_sizes):
            for overlap in parse_ints(args.overlaps):
                if overlap >= chunk_size:
                    continue
                chunks = splitter(args.docs, chunk_size, overlap)
                chunk_vectors = embed_documents([chunk.page_content for chunk in chunks])
                for top_k in parse_ints(args.top_ks):
                    metrics, errors = evaluate_config(
                        rows, query_vectors, chunks, chunk_vectors, top_k
                    )
                    key = (
                        f"splitter={splitter_name},chunk={chunk_size},"
                        f"overlap={overlap},k={top_k}"
                    )
                    experiments.append(
                        {
                            "splitter": splitter_name,
                            "chunk_size": chunk_size,
                            "overlap": overlap,
                            "top_k": top_k,
                            "chunks": len(chunks),
                            "metrics": metrics,
                        }
                    )
                    all_errors[key] = errors

    def score(item: dict) -> tuple[float, float, float]:
        k = item["top_k"]
        metrics = item["metrics"]
        return (
            metrics[f"hit_at_{k}"],
            metrics[f"mrr_at_{k}"],
            -metrics["p95_retrieval_latency_ms"],
        )

    best = max(experiments, key=score)
    review_counts = Counter(row.get("review_status", "unknown") for row in rows)
    report = {
        "generated_at": datetime.now().astimezone().isoformat(),
        "backend": args.backend,
        "dataset": str(args.dataset),
        "dataset_size": len(rows),
        "dataset_split": args.split,
        "label_review": dict(review_counts),
        "experiments": experiments,
        "best_experiment": best,
        "errors": all_errors,
    }
    REPORTS.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    json_path = REPORTS / f"rag_eval_{args.backend}_{stamp}.json"
    md_path = REPORTS / f"rag_eval_{args.backend}_{stamp}.md"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(markdown_report(report), encoding="utf-8")
    print(json_path)
    print(md_path)


if __name__ == "__main__":
    main()
