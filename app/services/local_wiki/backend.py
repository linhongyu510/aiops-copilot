"""本地 Wiki 检索后端适配。

将基于 SQLite FTS5/BM25 的 :class:`WikiStore` 包装为与
``hybrid_retrieval_service`` 集成的后端接口：只提供 BM25，不支持 dense。
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from app.models.retrieval import RetrievalCandidate

from .wiki_compiler import WikiCompiler
from .wiki_store import WikiPage, WikiStore

if TYPE_CHECKING:
    from app.services.vector_index_service import IndexingResult

__all__ = ["LocalWikiBackend"]

logger = logging.getLogger(__name__)

# 项目根目录：.../app/services/local_wiki/backend.py -> 向上四级。
_PROJECT_ROOT = Path(__file__).resolve().parents[3]


class LocalWikiBackend:
    """本地 Wiki（Markdown -> SQLite FTS5）检索后端。

    不引入新第三方依赖，仅使用标准库。初始化时不立即编译，由
    :meth:`initialize` 触发增量编译。
    """

    backend_type: str = "local_wiki"
    supports_dense: bool = False
    supports_bm25: bool = True

    def __init__(self, db_path: str | None = None, docs_dir: str | None = None):
        self._db_path = self._resolve(db_path or ".runtime/local_wiki.db")
        self._docs_dir = self._resolve(docs_dir or "aiops-docs")
        self._store: WikiStore | None = None
        self._compiler: WikiCompiler | None = None

    @staticmethod
    def _resolve(path: str) -> str:
        """相对路径解析为项目根目录下的绝对路径。"""
        p = Path(path)
        if not p.is_absolute():
            p = _PROJECT_ROOT / p
        return str(p)

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #
    def initialize(self) -> None:
        """打开存储并执行增量编译。"""
        self._store = WikiStore(self._db_path)
        self._compiler = WikiCompiler(self._store, self._docs_dir)
        result = self._compiler.compile_if_needed()
        logger.info(
            "local_wiki initialized: total=%d compiled=%d skipped=%d failed=%d in %dms",
            result.total_files,
            result.compiled,
            result.skipped,
            result.failed,
            result.duration_ms,
        )

    def close(self) -> None:
        """关闭底层存储。"""
        if self._store is not None:
            self._store.close()
        self._store = None
        self._compiler = None

    # ------------------------------------------------------------------ #
    # 检索
    # ------------------------------------------------------------------ #
    def bm25_search(self, query: str, top_k: int, branch: str) -> list[RetrievalCandidate]:
        """BM25 检索并转换为 RetrievalCandidate 列表。"""
        if self._store is None:
            return []
        hits = self._store.search(query, top_k)
        candidates: list[RetrievalCandidate] = []
        for hit in hits:
            page = self._store.get_page(hit.page_id)
            content = hit.content_snippet
            section = next((s for s in page.sections if s.seq == hit.section_seq), None)
            if section is not None and section.content:
                content = section.content
            candidates.append(
                RetrievalCandidate(
                    chunk_id=f"{hit.page_id}#{hit.section_seq}",
                    content=content,
                    metadata={
                        "source": hit.source,
                        "title": hit.title,
                        "section": hit.section_heading,
                        "_file_name": hit.source,
                        "_source": hit.source,
                        "backend": "local_wiki",
                    },
                    branch_ranks={branch: hit.rank},
                    raw_scores={branch: float(hit.score)},
                )
            )
        return candidates

    def dense_search(
        self, vector: list[float], top_k: int, branch: str
    ) -> list[RetrievalCandidate]:
        """本地 Wiki 不支持稠密向量检索，始终返回空列表。"""
        return []

    # ------------------------------------------------------------------ #
    # 索引
    # ------------------------------------------------------------------ #
    def index_file(self, file_path: str) -> None:
        """编译单个文件到 Wiki 存储。"""
        if self._compiler is None or self._store is None:
            self.initialize()
        assert self._compiler is not None and self._store is not None
        page: WikiPage = self._compiler.compile_file(Path(file_path))
        self._store.upsert_page(page)

    def index_directory(self, directory_path: str | None = None) -> IndexingResult:
        """编译目录下所有 Markdown 文件，返回兼容的 IndexingResult。"""
        from app.services.vector_index_service import IndexingResult

        result = IndexingResult()
        result.directory_path = directory_path or self._docs_dir
        result.start_time = datetime.now()
        try:
            if self._compiler is None or self._docs_dir != self._resolve(
                directory_path or self._docs_dir
            ):
                # 目录变化时用指定目录构造编译器。
                docs_dir = self._resolve(directory_path) if directory_path else self._docs_dir
                if self._store is None:
                    self._store = WikiStore(self._db_path)
                self._compiler = WikiCompiler(self._store, docs_dir)
            cr = self._compiler.compile_all()
            result.total_files = cr.total_files
            result.success_count = cr.compiled
            result.fail_count = cr.failed
            result.success = cr.failed == 0
            if cr.failed:
                result.error_message = f"{cr.failed} file(s) failed to compile"
        except Exception as exc:  # noqa: BLE001 - 对外统一兜底
            result.success = False
            result.error_message = str(exc)
            logger.exception("index_directory failed")
        finally:
            result.end_time = datetime.now()
        return result

    # ------------------------------------------------------------------ #
    # 健康
    # ------------------------------------------------------------------ #
    def health(self) -> dict[str, Any]:
        """返回后端健康状态。"""
        if self._store is None:
            return {"status": "not_ready", "pages": 0, "message": "backend not initialized"}
        try:
            n = self._store.page_count()
            status = "ready" if n > 0 else "not_ready"
            return {
                "status": status,
                "pages": n,
                "message": f"{n} wiki pages indexed",
            }
        except Exception as exc:  # noqa: BLE001
            return {"status": "not_ready", "pages": 0, "message": str(exc)}
