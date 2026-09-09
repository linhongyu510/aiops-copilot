"""Ordered RAG v2 ablation suite; test split requires an explicit lock acknowledgement."""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

from app.config import config
from app.core.milvus_client import milvus_manager
from app.services.hybrid_retrieval_service import (
    RetrievalOptions,
    hybrid_retrieval_service,
)
from evaluation.rag_v2_eval import (
    DATASET,
    MANIFEST,
    REPORTS,
    bootstrap_ci,
    evaluate,
    load_rows,
    run_vector,
    runtime_chunks,
    validate_runtime_contract,
)

EXPERIMENTS = (
    (
        "dense_large",
        RetrievalOptions(
            rewrite=False, multi_query=False, hyde=False, bm25=False, rrf=False, reranker=False
        ),
    ),
    (
        "rewrite",
        RetrievalOptions(
            rewrite=True, multi_query=False, hyde=False, bm25=False, rrf=False, reranker=False
        ),
    ),
    (
        "multi_query",
        RetrievalOptions(
            rewrite=True, multi_query=True, hyde=False, bm25=False, rrf=False, reranker=False
        ),
    ),
    (
        "hyde",
        RetrievalOptions(
            rewrite=True, multi_query=True, hyde=True, bm25=False, rrf=False, reranker=False
        ),
    ),
    (
        "bm25",
        RetrievalOptions(
            rewrite=True, multi_query=True, hyde=True, bm25=True, rrf=False, reranker=False
        ),
    ),
    (
        "rrf",
        RetrievalOptions(
            rewrite=True, multi_query=True, hyde=True, bm25=True, rrf=True, reranker=False
        ),
    ),
    (
        "reranker",
        RetrievalOptions(
            rewrite=True, multi_query=True, hyde=True, bm25=True, rrf=True, reranker=True
        ),
    ),
    ("full", RetrievalOptions()),
)


def reset_peak_gpu_memory() -> None:
    try:
        import torch

        if torch.cuda.is_available():
            torch.cuda.reset_peak_memory_stats()
    except ImportError:
        pass


def peak_gpu_memory_mb() -> float:
    try:
        import torch

        if torch.cuda.is_available():
            torch.cuda.synchronize()
            return round(torch.cuda.max_memory_allocated() / 1024 / 1024, 2)
    except ImportError:
        pass
    return 0.0


def confidence_intervals(rows: list[dict], rankings: list[list[str]]) -> dict:
    return {
        metric: bootstrap_ci(rows, rankings, metric)
        for metric in ("recall_at_20", "mrr_at_10", "ndcg_at_10")
    }


async def rank_rows(rows: list[dict], options: RetrievalOptions) -> tuple[list[list[str]], list[float]]:
    rankings: list[list[str]] = []
    latencies: list[float] = []
    for row in rows:
        started = time.perf_counter()
        candidates, _ = await hybrid_retrieval_service.search(
            row["query"],
            20,
            options=options,
        )
        latencies.append((time.perf_counter() - started) * 1000)
        rankings.append(
            [
                str(
                    candidate.metadata.get("_file_name")
                    or candidate.metadata.get("source")
                    or candidate.metadata.get("_source")
                )
                for candidate in candidates
            ]
        )
    return rankings, latencies


async def run(args: argparse.Namespace) -> Path:
    rows = load_rows(args.dataset, args.split)
    if args.sample:
        rows = rows[: args.sample]
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    validate_runtime_contract(manifest)
    chunks = runtime_chunks()
    from app.services.vector_embedding_service import LocalSentenceTransformerEmbeddings

    small_embedding = LocalSentenceTransformerEmbeddings(
        model_name="BAAI/bge-small-zh-v1.5",
        dimensions=512,
        device=config.local_embedding_device,
        batch_size=config.local_embedding_batch_size,
        cache_dir=config.local_embedding_cache_dir,
        source=config.local_embedding_source,
        modelscope_model_id="BAAI/bge-small-zh-v1.5",
        revision="7999e1d3359715c523056ef9478215996d62a620",
    )
    reset_peak_gpu_memory()
    small_started = time.perf_counter()
    small_rankings = run_vector(
        rows,
        chunks,
        small_embedding.embed_documents,
        small_embedding.embed_queries,
    )
    small_metrics, small_errors = evaluate(rows, small_rankings)
    small_elapsed = (time.perf_counter() - small_started) * 1000
    results = [
        {
            "name": "dense_small",
            "options": {"model": "BAAI/bge-small-zh-v1.5"},
            "metrics": small_metrics,
            "bootstrap_95_ci": confidence_intervals(rows, small_rankings),
            "total_latency_ms": round(small_elapsed, 2),
            "p50_latency_ms": 0.0,
            "p95_latency_ms": 0.0,
            "peak_gpu_memory_mb": peak_gpu_memory_mb(),
            "error_attribution": dict(
                Counter(error["error_type"] for error in small_errors)
            ),
        }
    ]
    milvus_manager.connect()
    try:
        for name, options in EXPERIMENTS:
            reset_peak_gpu_memory()
            rankings, latencies = await rank_rows(rows, options)
            metrics, errors = evaluate(rows, rankings)
            ordered = sorted(latencies)
            results.append(
                {
                    "name": name,
                    "options": options.__dict__,
                    "metrics": metrics,
                    "bootstrap_95_ci": confidence_intervals(rows, rankings),
                    "p50_latency_ms": ordered[len(ordered) // 2] if ordered else 0.0,
                    "p95_latency_ms": ordered[max(0, int(len(ordered) * 0.95) - 1)]
                    if ordered
                    else 0.0,
                    "peak_gpu_memory_mb": peak_gpu_memory_mb(),
                    "error_attribution": dict(
                        Counter(error["error_type"] for error in errors)
                    ),
                }
            )
    finally:
        milvus_manager.close()
    baseline = next(item["metrics"] for item in results if item["name"] == "dense_large")
    final = results[-1]["metrics"]
    quality_gate = {
        metric: final[metric] > baseline[metric]
        for metric in ("recall_at_20", "mrr_at_10", "ndcg_at_10")
    }
    pre_reranker = next(item["metrics"] for item in results if item["name"] == "rrf")
    reranker = next(item["metrics"] for item in results if item["name"] == "reranker")
    reranker_gate = (
        reranker["mrr_at_10"] > pre_reranker["mrr_at_10"]
        or reranker["ndcg_at_10"] > pre_reranker["ndcg_at_10"]
    )
    report = {
        "generated_at": datetime.now().astimezone().isoformat(),
        "dataset": str(args.dataset),
        "split": args.split,
        "dataset_size": len(rows),
        "corpus_id": manifest["corpus_id"],
        "corpus_hash": manifest["corpus_hash"],
        "embedding_model": config.local_embedding_model,
        "embedding_revision": config.local_embedding_revision,
        "reranker_model": config.rag_reranker_model,
        "reranker_revision": config.rag_reranker_revision,
        "random_seed": 20260730,
        "review_status": dict(Counter(row["review_status"] for row in rows)),
        "experiments": results,
        "quality_gate": quality_gate,
        "reranker_gate": {
            "mrr_or_ndcg_improved_over_rrf": reranker_gate,
        },
        "quality_gate_passed": all(quality_gate.values()) and reranker_gate,
        "evidence_warning": "Pending labels cannot be represented as human annotations.",
    }
    REPORTS.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    output = REPORTS / f"rag_v2_ablation_{args.split}_{stamp}.json"
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return output


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", type=Path, default=DATASET)
    parser.add_argument("--split", choices=["dev", "test"], default="dev")
    parser.add_argument("--sample", type=int)
    parser.add_argument("--confirm-locked-test", action="store_true")
    args = parser.parse_args()
    if args.split == "test" and not args.confirm_locked_test:
        raise SystemExit(
            "test split 只能在配置锁定后运行；请显式传入 --confirm-locked-test"
        )
    if args.split == "test" and args.sample:
        raise SystemExit("锁定 Test 必须运行完整集合，不能使用 --sample")
    if args.split == "test" and any(REPORTS.glob("rag_v2_ablation_test_*.json")):
        raise SystemExit("已存在锁定 Test 报告；为避免重复窥视 Test，拒绝再次运行")
    print(asyncio.run(run(args)))


if __name__ == "__main__":
    main()
