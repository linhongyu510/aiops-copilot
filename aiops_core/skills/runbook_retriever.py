"""Runbook 检索：Skill 显式声明的 wiki 文档 → 按 h2 小节 → BM25 打分抽取。

从 ``app.agent.skills.runbook_retriever`` 抽出。字面等价，仅把默认 ``docs_dir``
计算方式改为相对 ``aiops_core/skills/`` 位置的 project_root。
"""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class RunbookSection:
    """一篇 runbook 的一个 h2 小节。"""

    doc_id: str
    doc_title: str
    section_title: str
    content: str
    file_path: str

    def as_prompt_block(self) -> str:
        return (
            f"【参考：{self.doc_title} / {self.section_title}】\n"
            f"（来源：aiops-docs/{self.doc_id}.md）\n"
            f"{self.content.strip()}"
        )


@dataclass
class _CachedDoc:
    mtime: float
    doc_title: str
    sections: list[RunbookSection] = field(default_factory=list)


class RunbookRetriever:
    """按 doc_id 加载 runbook 并按 step 描述打分抽取最相关的 h2 小节。"""

    _H2 = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)
    _TOKEN = re.compile(r"[\u4e00-\u9fff]|[a-zA-Z0-9_]+")

    def __init__(self, docs_dir: Path):
        self._docs_dir = docs_dir
        self._cache: dict[str, _CachedDoc] = {}
        self._lock = threading.Lock()

    def load_sections(self, doc_id: str) -> list[RunbookSection]:
        path = self._docs_dir / f"{doc_id}.md"
        if not path.is_file():
            return []
        try:
            mtime = path.stat().st_mtime
        except OSError:
            return []

        with self._lock:
            cached = self._cache.get(doc_id)
            if cached and cached.mtime == mtime:
                return cached.sections

        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            return []

        doc_title = self._extract_h1(text) or doc_id
        sections = self._split_by_h2(text, doc_id=doc_id, doc_title=doc_title, path=path)

        with self._lock:
            self._cache[doc_id] = _CachedDoc(
                mtime=mtime, doc_title=doc_title, sections=sections
            )
        return sections

    def extract_relevant(
        self,
        doc_ids: list[str],
        query: str,
        *,
        top_k: int = 3,
        max_chars_per_section: int = 800,
    ) -> list[RunbookSection]:
        query_tokens = self._tokenize(query)
        if not query_tokens:
            return []

        candidates: list[tuple[float, RunbookSection]] = []
        for doc_id in doc_ids:
            for section in self.load_sections(doc_id):
                score = self._score(section, query_tokens)
                if score > 0:
                    candidates.append((score, section))

        candidates.sort(key=lambda item: item[0], reverse=True)
        result: list[RunbookSection] = []
        for _, section in candidates[:top_k]:
            if len(section.content) > max_chars_per_section:
                trimmed = section.content[:max_chars_per_section] + "……[已截断]"
                result.append(
                    RunbookSection(
                        doc_id=section.doc_id,
                        doc_title=section.doc_title,
                        section_title=section.section_title,
                        content=trimmed,
                        file_path=section.file_path,
                    )
                )
            else:
                result.append(section)
        return result

    def _extract_h1(self, text: str) -> str:
        for line in text.splitlines():
            if line.startswith("# ") and not line.startswith("##"):
                return line[2:].strip()
        return ""

    def _split_by_h2(
        self, text: str, *, doc_id: str, doc_title: str, path: Path
    ) -> list[RunbookSection]:
        matches = list(self._H2.finditer(text))
        if not matches:
            return [
                RunbookSection(
                    doc_id=doc_id,
                    doc_title=doc_title,
                    section_title=doc_title,
                    content=text,
                    file_path=str(path),
                )
            ]
        sections: list[RunbookSection] = []
        for i, m in enumerate(matches):
            start = m.start()
            end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
            body = text[start:end].strip()
            title = m.group(1).strip()
            sections.append(
                RunbookSection(
                    doc_id=doc_id,
                    doc_title=doc_title,
                    section_title=title,
                    content=body,
                    file_path=str(path),
                )
            )
        return sections

    def _tokenize(self, text: str) -> list[str]:
        return [t.lower() for t in self._TOKEN.findall(text) if t]

    def _score(self, section: RunbookSection, query_tokens: list[str]) -> float:
        section_tokens = self._tokenize(section.content)
        if not section_tokens:
            return 0.0
        title_tokens = set(self._tokenize(section.section_title))
        title_hits = sum(2.0 for t in query_tokens if t in title_tokens)
        body_freq: dict[str, int] = {}
        for tok in section_tokens:
            body_freq[tok] = body_freq.get(tok, 0) + 1
        body_hits = sum(body_freq.get(t, 0) for t in query_tokens)
        return (title_hits + body_hits) / (1 + len(section_tokens) ** 0.5)


_default_retriever: RunbookRetriever | None = None
_default_lock = threading.Lock()


def _default_docs_dir() -> Path:
    """默认 wiki 目录 = 项目根 / aiops-docs。

    aiops_core/skills/runbook_retriever.py → parents[2] 即项目根。
    """
    return Path(__file__).resolve().parents[2] / "aiops-docs"


def get_runbook_retriever() -> RunbookRetriever:
    """项目默认单例：指向 <project_root>/aiops-docs/。"""
    global _default_retriever
    with _default_lock:
        if _default_retriever is None:
            _default_retriever = RunbookRetriever(_default_docs_dir())
        return _default_retriever
