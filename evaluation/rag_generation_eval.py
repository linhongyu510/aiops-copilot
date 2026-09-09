"""End-to-end answer grounding, citation and relevance evaluation."""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import statistics
import time
from datetime import datetime
from pathlib import Path

import httpx
from langchain_core.messages import HumanMessage

from app.config import config
from app.core.llm_factory import llm_factory
from evaluation.rag_eval import _tokens
from evaluation.rag_v2_eval import DATASET, MANIFEST, REPORTS, load_rows


def token_coverage(reference: str, candidate: str) -> float:
    reference_tokens = set(_tokens(reference))
    if not reference_tokens:
        return 1.0
    return len(reference_tokens & set(_tokens(candidate))) / len(reference_tokens)


def deterministic_scores(
    row: dict,
    answer: str,
    contexts: list[str],
) -> dict:
    citations = [int(value) for value in re.findall(r"\[(\d+)\]", answer)]
    valid_citations = [value for value in citations if 1 <= value <= len(contexts)]
    key_facts = row.get("key_facts", [])
    covered_facts = [
        fact for fact in key_facts if token_coverage(fact, answer) >= 0.35
    ]
    context = "\n".join(contexts)
    answer_tokens = set(_tokens(re.sub(r"\[\d+\]", "", answer)))
    context_tokens = set(_tokens(context))
    grounded = len(answer_tokens & context_tokens) / len(answer_tokens) if answer_tokens else 0.0
    return {
        "key_fact_coverage": round(
            len(covered_facts) / len(key_facts), 4
        ) if key_facts else None,
        "citation_presence": bool(citations),
        "citation_validity": round(
            len(valid_citations) / len(citations), 4
        ) if citations else 0.0,
        "context_token_support": round(grounded, 4),
        "answer_relevance": round(token_coverage(row["query"], answer), 4),
        "covered_key_facts": covered_facts,
    }


def parse_judge(content: object) -> dict[str, float]:
    text = str(content)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("judge 未返回 JSON")
    payload = json.loads(text[start : end + 1])
    return {
        name: min(1.0, max(0.0, float(payload[name])))
        for name in ("faithfulness", "answer_relevance")
    }


async def judge_answer(model, row: dict, answer: str, contexts: list[str]) -> dict:
    response = await model.ainvoke(
        [
            HumanMessage(
                content=(
                    "你是 RAG 离线评测器。只输出 JSON，不输出推理过程。"
                    "分别给出 0 到 1 的 faithfulness 和 answer_relevance。"
                    f"\n问题：{row['query']}\n答案：{answer}"
                    f"\n检索证据：{json.dumps(contexts, ensure_ascii=False)}"
                )
            )
        ]
    )
    return parse_judge(response.content)


async def run(args: argparse.Namespace) -> dict:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    rows = load_rows(args.dataset, args.split)
    rows = [row for row in rows if row["expected_sources"]]
    if args.sample:
        rows = rows[: args.sample]
    results = []
    judge_model = (
        llm_factory.create_chat_model(
            model=args.judge_model,
            temperature=0,
            streaming=False,
            max_tokens=120,
        )
        if args.llm_judge
        else None
    )
    async with httpx.AsyncClient(timeout=httpx.Timeout(args.timeout)) as client:
        for index, row in enumerate(rows, start=1):
            started = time.perf_counter()
            retrieval = await client.post(
                f"{args.base_url.rstrip('/')}/api/rag/search",
                json={"query": row["query"], "top_k": 5},
            )
            retrieval.raise_for_status()
            retrieved = retrieval.json().get("candidates", [])
            contexts = [item.get("content", "") for item in retrieved]
            response = await client.post(
                f"{args.base_url.rstrip('/')}/api/chat",
                json={
                    "Id": f"rag-generation-eval-{row['id']}",
                    "Question": f"请根据内部运维知识库回答：{row['query']}",
                },
            )
            response.raise_for_status()
            payload = response.json()
            answer = payload.get("data", {}).get("answer") or payload.get("answer") or ""
            scores = deterministic_scores(row, answer, contexts)
            judgment = None
            judge_error = None
            if judge_model is not None and index <= args.judge_sample_size:
                try:
                    judgment = await judge_answer(judge_model, row, answer, contexts)
                except Exception as exc:
                    judge_error = type(exc).__name__
            results.append(
                {
                    "id": row["id"],
                    "query": row["query"],
                    "answer": answer,
                    "retrieved_sources": [
                        item.get("metadata", {}).get("_file_name") for item in retrieved
                    ],
                    "latency_ms": round((time.perf_counter() - started) * 1000, 2),
                    **scores,
                    "llm_judge": judgment,
                    "llm_judge_error": judge_error,
                    "review_status": "pending",
                }
            )
            print(f"{index}/{len(rows)} {row['id']}", flush=True)
    numeric = (
        "key_fact_coverage",
        "citation_validity",
        "context_token_support",
        "answer_relevance",
    )
    aggregate = {
        name: round(
            statistics.mean(
                float(item[name]) for item in results if item[name] is not None
            ),
            4,
        )
        if results
        else 0.0
        for name in numeric
    }
    aggregate["citation_presence_rate"] = round(
        statistics.mean(float(item["citation_presence"]) for item in results), 4
    ) if results else 0.0
    judged = [item["llm_judge"] for item in results if item["llm_judge"]]
    aggregate["llm_judge_faithfulness"] = (
        round(statistics.mean(item["faithfulness"] for item in judged), 4)
        if judged
        else None
    )
    aggregate["llm_judge_answer_relevance"] = (
        round(statistics.mean(item["answer_relevance"] for item in judged), 4)
        if judged
        else None
    )
    return {
        "generated_at": datetime.now().astimezone().isoformat(),
        "dataset": str(args.dataset),
        "split": args.split,
        "samples": len(results),
        "corpus_id": manifest["corpus_id"],
        "corpus_hash": manifest["corpus_hash"],
        "embedding_model": manifest["embedding"]["default_model"],
        "embedding_revision": manifest["embedding"]["revision"],
        "reranker_model": manifest["retrieval"]["reranker"],
        "reranker_revision": manifest["retrieval"]["reranker_revision"],
        "random_seed": 20260730,
        "metrics": aggregate,
        "judge_model": args.judge_model if args.llm_judge else None,
        "judge_sample_size": len(judged),
        "human_review_required": True,
        "human_spot_check_guidance": "随机抽检不少于 judge 样本的 20%，并记录 reviewer 与异议。",
        "results": results,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", type=Path, default=DATASET)
    parser.add_argument("--split", choices=["dev", "test"], default="dev")
    parser.add_argument("--base-url", default="http://127.0.0.1:9900")
    parser.add_argument("--sample", type=int, default=20)
    parser.add_argument("--timeout", type=float, default=180.0)
    parser.add_argument("--llm-judge", action="store_true")
    parser.add_argument("--judge-model", default=config.rag_expansion_model)
    parser.add_argument("--judge-sample-size", type=int, default=20)
    args = parser.parse_args()
    report = asyncio.run(run(args))
    REPORTS.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    output = REPORTS / f"rag_generation_{args.split}_{stamp}.json"
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(output)


if __name__ == "__main__":
    main()
