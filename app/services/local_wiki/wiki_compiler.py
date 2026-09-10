"""将 Markdown Runbook 编译为 Wiki 页面。

遍历 docs_dir 下的 ``.md`` 文件，解析标题与二级章节，结合
CORPUS_MANIFEST.json 中的分类信息，产出 :class:`WikiPage` 并写入
:class:`WikiStore`。支持基于文件内容 hash 的增量编译。
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path

from .tokenizer import tokenize
from .wiki_store import WikiPage, WikiSection, WikiStore

__all__ = ["CompileResult", "WikiCompiler"]

logger = logging.getLogger(__name__)


@dataclass
class CompileResult:
    """一次编译的统计结果。"""

    total_files: int = 0
    compiled: int = 0
    skipped: int = 0
    failed: int = 0
    duration_ms: int = 0


class WikiCompiler:
    """Markdown -> Wiki 编译器。

    参数:
        store: 已打开的 :class:`WikiStore`。
        docs_dir: Markdown 文档目录。
    """

    def __init__(self, store: WikiStore, docs_dir: str):
        self.store = store
        self.docs_dir = Path(docs_dir)
        self._category_map = self._load_manifest()

    # ------------------------------------------------------------------ #
    # manifest
    # ------------------------------------------------------------------ #
    def _load_manifest(self) -> dict[str, str]:
        """从 CORPUS_MANIFEST.json 构建 file -> category 映射。"""
        manifest_path = self.docs_dir / "CORPUS_MANIFEST.json"
        if not manifest_path.exists():
            return {}
        try:
            data = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            logger.warning("无法解析 %s，category 将回退为 general", manifest_path)
            return {}
        mapping: dict[str, str] = {}
        for doc in data.get("documents", []):
            file_name = doc.get("file")
            category = doc.get("category")
            if file_name and category:
                mapping[file_name] = category
        return mapping

    # ------------------------------------------------------------------ #
    # markdown 解析
    # ------------------------------------------------------------------ #
    @staticmethod
    def _parse_markdown(text: str, file_path: Path) -> tuple[str, list[WikiSection]]:
        """解析 Markdown 文本为 (title, sections)。

        - 首个 ``# `` 一级标题作为页面 title。
        - ``## `` 二级标题开启新章节；``###`` 及以下归入当前章节。
        - 代码块围栏内的行不作为标题解析。
        """
        title = file_path.stem
        sections: list[WikiSection] = []
        current_heading = ""
        current_lines: list[str] = []
        seq = 0
        in_fence = False

        def flush() -> None:
            nonlocal seq
            body = "\n".join(current_lines).strip()
            # 跳过既无标题也无正文的空章节。
            if current_heading or body:
                sections.append(WikiSection(heading=current_heading, content=body, seq=seq))
                seq += 1

        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("```") or stripped.startswith("~~~"):
                in_fence = not in_fence
                current_lines.append(line)
                continue

            if not in_fence and line.startswith("# "):
                title = line[2:].strip()
                continue
            if not in_fence and line.startswith("## "):
                flush()
                current_heading = line[3:].strip()
                current_lines = []
                continue
            current_lines.append(line)

        flush()
        return title, sections

    # ------------------------------------------------------------------ #
    # 单文件编译
    # ------------------------------------------------------------------ #
    def compile_file(self, file_path: Path) -> WikiPage:
        """编译单个 Markdown 文件为 WikiPage（不写入存储）。"""
        text = file_path.read_text(encoding="utf-8")
        content_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
        title, sections = self._parse_markdown(text, file_path)
        source = file_path.relative_to(self.docs_dir).as_posix()
        category = self._category_map.get(file_path.name, "general")
        return WikiPage(
            id=file_path.stem,
            source=source,
            title=title,
            category=category,
            content=text,
            sections=sections,
            related_pages=[],
            content_hash=content_hash,
        )

    # ------------------------------------------------------------------ #
    # related pages
    # ------------------------------------------------------------------ #
    @staticmethod
    def _heading_token_sets(pages: list[WikiPage]) -> dict[str, set[str]]:
        """计算每个页面（基于标题与章节标题）的 token 集合。"""
        token_sets: dict[str, set[str]] = {}
        for p in pages:
            toks = set(tokenize(p.title))
            for sec in p.sections:
                toks.update(tokenize(sec.heading))
            token_sets[p.id] = toks
        return token_sets

    def _compute_related(self, pages: list[WikiPage], top_k: int = 5) -> None:
        """基于标题/章节 token 重叠，为每个页面填充 related_pages（确定性）。"""
        if not pages:
            return
        token_sets = self._heading_token_sets(pages)
        for page in pages:
            mine = token_sets.get(page.id, set())
            scored: list[tuple[int, str]] = []
            for other in pages:
                if other.id == page.id:
                    continue
                overlap = len(mine & token_sets.get(other.id, set()))
                if overlap > 0:
                    scored.append((overlap, other.id))
            # 重叠数降序，再按 id 升序保证确定性。
            scored.sort(key=lambda item: (-item[0], item[1]))
            page.related_pages = [pid for _, pid in scored[:top_k]]

    # ------------------------------------------------------------------ #
    # 全量 / 增量编译
    # ------------------------------------------------------------------ #
    def _markdown_files(self) -> list[Path]:
        """列出 docs_dir 下全部 .md 文件（排序以保证确定性）。"""
        if not self.docs_dir.exists():
            return []
        return sorted(p for p in self.docs_dir.rglob("*.md") if p.is_file())

    def compile_all(self) -> CompileResult:
        """全量编译：忽略现有 hash，重新编译并入库，清理失效页面。"""
        start = time.perf_counter()
        files = self._markdown_files()
        pages: list[WikiPage] = []
        compiled = 0
        failed = 0
        for fp in files:
            try:
                pages.append(self.compile_file(fp))
                compiled += 1
            except Exception:
                logger.exception("编译文件失败: %s", fp)
                failed += 1

        self._compute_related(pages)
        for page in pages:
            self.store.upsert_page(page)

        # 清理已不存在的页面。
        current_ids = {p.id for p in pages}
        for existing in self.store.list_pages():
            if existing.id not in current_ids:
                self.store.delete_page(existing.id)

        self.store.set_meta("docs_dir", str(self.docs_dir.resolve()))
        return CompileResult(
            total_files=len(files),
            compiled=compiled,
            skipped=0,
            failed=failed,
            duration_ms=int((time.perf_counter() - start) * 1000),
        )

    def compile_if_needed(self) -> CompileResult:
        """增量编译：仅重新编译内容 hash 变化的文件。

        当数据库为空、或 docs_dir 路径发生变化时，退化为全量编译。
        """
        start = time.perf_counter()
        files = self._markdown_files()
        total = len(files)

        stored_dir = self.store.get_meta("docs_dir")
        current_dir = str(self.docs_dir.resolve())
        if self.store.page_count() == 0 or stored_dir != current_dir:
            result = self.compile_all()
            result.duration_ms = int((time.perf_counter() - start) * 1000)
            return result

        changed: list[WikiPage] = []
        compiled = 0
        skipped = 0
        failed = 0
        changed_ids: set[str] = set()

        for fp in files:
            try:
                text = fp.read_text(encoding="utf-8")
                chash = hashlib.sha256(text.encode("utf-8")).hexdigest()
                existing = self.store.get_page(fp.stem)
                if existing is not None and existing.content_hash == chash:
                    skipped += 1
                    continue
                page = self.compile_file(fp)
                changed.append(page)
                changed_ids.add(page.id)
                compiled += 1
            except Exception:
                logger.exception("编译文件失败: %s", fp)
                failed += 1

        # 相关页计算需覆盖库中全部页面（含未变更者）。
        all_pages = changed + [p for p in self.store.list_pages() if p.id not in changed_ids]
        self._compute_related(all_pages)

        for page in changed:
            self.store.upsert_page(page)

        # 清理已删除文件对应的页面。
        current_ids = {fp.stem for fp in files}
        for existing in self.store.list_pages():
            if existing.id not in current_ids:
                self.store.delete_page(existing.id)

        self.store.set_meta("docs_dir", current_dir)
        return CompileResult(
            total_files=total,
            compiled=compiled,
            skipped=skipped,
            failed=failed,
            duration_ms=int((time.perf_counter() - start) * 1000),
        )
