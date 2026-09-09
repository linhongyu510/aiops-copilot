"""字符 n-gram TF-IDF 余弦相似度（零依赖版本）。

从 ``app.agent.text_similarity`` 抽出。原文件已无外部依赖。
"""

from __future__ import annotations

import math
import re
from collections import Counter

_CJK_RUN = re.compile(r"[\u4e00-\u9fff]+")
_ASCII_WORD = re.compile(r"[a-z0-9]+")


def tokenize(text: str) -> list[str]:
    """中英混合分词：ASCII 词 + CJK 二元组（bigram）。"""
    lowered = text.casefold()
    tokens: list[str] = _ASCII_WORD.findall(lowered)
    for run in _CJK_RUN.findall(lowered):
        if len(run) == 1:
            tokens.append(run)
        else:
            tokens.extend(run[i : i + 2] for i in range(len(run) - 1))
    return tokens


class TfidfIndex:
    """对小规模文档集合做 TF-IDF 余弦检索。"""

    def __init__(self, documents: list[str]):
        self._tf: list[Counter[str]] = [Counter(tokenize(doc)) for doc in documents]
        document_frequency: Counter[str] = Counter()
        for term_counts in self._tf:
            document_frequency.update(term_counts.keys())
        total = max(len(documents), 1)
        self._idf: dict[str, float] = {
            term: math.log((1 + total) / (1 + count)) + 1.0
            for term, count in document_frequency.items()
        }

    def __len__(self) -> int:
        return len(self._tf)

    def score(self, query: str) -> list[float]:
        query_counts = Counter(term for term in tokenize(query) if term in self._idf)
        if not query_counts:
            return [0.0] * len(self._tf)
        query_weight = {term: count * self._idf[term] for term, count in query_counts.items()}
        query_norm = math.sqrt(sum(w * w for w in query_weight.values()))
        scores: list[float] = []
        for term_counts in self._tf:
            doc_norm = math.sqrt(
                sum((count * self._idf[term]) ** 2 for term, count in term_counts.items())
            )
            if doc_norm == 0 or query_norm == 0:
                scores.append(0.0)
                continue
            dot = sum(
                query_weight[term] * count * self._idf[term]
                for term, count in term_counts.items()
                if term in query_weight
            )
            scores.append(dot / (query_norm * doc_norm))
        return scores

    def top_k(self, query: str, k: int, min_score: float = 0.0) -> list[tuple[int, float]]:
        scored = sorted(
            (
                (index, score)
                for index, score in enumerate(self.score(query))
                if score >= min_score
            ),
            key=lambda pair: -pair[1],
        )
        return scored[:k]
