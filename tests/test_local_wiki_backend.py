"""LocalWikiBackend 适配层单元测试。

区分两类用例：
- 临时目录隔离用例：自构造 LocalWikiBackend(tmp_db, tmp_docs)，不污染真实库；
- 真实 aiops-docs 用例：使用默认路径初始化，验证端到端 BM25 召回。
"""

from __future__ import annotations

from pathlib import Path

from app.services.local_wiki.backend import LocalWikiBackend


def test_backend_type_and_capabilities() -> None:
    backend = LocalWikiBackend(db_path=":memory:")
    assert backend.backend_type == "local_wiki"
    assert backend.supports_dense is False
    assert backend.supports_bm25 is True


def test_initialize_creates_db(tmp_path: Path) -> None:
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "alpha.md").write_text("# Alpha\n## S\nCPU 使用率过高\n", encoding="utf-8")
    backend = LocalWikiBackend(db_path=str(tmp_path / "w.db"), docs_dir=str(docs))
    try:
        backend.initialize()
        assert backend.health()["status"] == "ready"
    finally:
        backend.close()


def test_health_before_initialize(tmp_path: Path) -> None:
    backend = LocalWikiBackend(db_path=str(tmp_path / "w.db"), docs_dir=str(tmp_path / "docs"))
    try:
        status = backend.health()["status"]
        assert status != "ready"
    finally:
        backend.close()


def test_close(tmp_path: Path) -> None:
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "a.md").write_text("# A\n## S\nbody\n", encoding="utf-8")
    backend = LocalWikiBackend(db_path=str(tmp_path / "w.db"), docs_dir=str(docs))
    backend.initialize()
    backend.close()
    # close 后再调用 health 不应崩溃。
    assert backend.health()["status"] == "not_ready"


def test_dense_search_returns_empty(tmp_path: Path) -> None:
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "a.md").write_text("# A\n## S\nbody\n", encoding="utf-8")
    backend = LocalWikiBackend(db_path=str(tmp_path / "w.db"), docs_dir=str(docs))
    try:
        backend.initialize()
        assert backend.dense_search([0.1, 0.2], 5, "dense_test") == []
    finally:
        backend.close()


def test_index_file(tmp_path: Path) -> None:
    docs = tmp_path / "docs"
    docs.mkdir()
    backend = LocalWikiBackend(db_path=str(tmp_path / "w.db"), docs_dir=str(docs))
    try:
        backend.initialize()
        md = docs / "runbook.md"
        md.write_text("# Runbook\n## S\nCPU 使用率过高导致雪崩\n", encoding="utf-8")
        backend.index_file(str(md))
        cands = backend.bm25_search("CPU 使用率", 5, "bm25_original")
        assert cands
        assert "runbook" in cands[0].metadata["source"]
    finally:
        backend.close()


def test_index_directory(tmp_path: Path) -> None:
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "a.md").write_text("# A\n## S\nCPU 使用率\n", encoding="utf-8")
    (docs / "b.md").write_text("# B\n## S\n内存泄漏\n", encoding="utf-8")
    backend = LocalWikiBackend(db_path=str(tmp_path / "w.db"), docs_dir=str(docs))
    try:
        backend.initialize()
        result = backend.index_directory()
        assert result.success is True
        assert result.success_count > 0
        cands = backend.bm25_search("CPU 使用率", 5, "bm25_original")
        assert any("a.md" in c.metadata["source"] for c in cands)
    finally:
        backend.close()


# --- 真实 aiops-docs 端到端用例 ------------------------------------------- #
def _real_backend() -> LocalWikiBackend:
    """构造并初始化指向真实 aiops-docs 的后端（默认路径，库已预编译）。"""
    backend = LocalWikiBackend()
    backend.initialize()
    return backend


def test_bm25_search_returns_candidates() -> None:
    backend = _real_backend()
    try:
        cands = backend.bm25_search("CPU 使用率过高", 5, "bm25_original")
        assert isinstance(cands, list)
        assert len(cands) >= 1
    finally:
        backend.close()


def test_bm25_search_cpu_hit_has_source() -> None:
    backend = _real_backend()
    try:
        cands = backend.bm25_search("CPU 使用率过高", 10, "bm25_original")
        assert any("cpu_high_usage" in c.metadata["source"] for c in cands)
    finally:
        backend.close()


def test_bm25_search_metadata_fields() -> None:
    backend = _real_backend()
    try:
        cands = backend.bm25_search("CPU 使用率过高", 5, "bm25_original")
        assert cands
        meta = cands[0].metadata
        for key in ("source", "title", "section", "_file_name", "_source", "backend"):
            assert key in meta, f"缺少 metadata 字段: {key}"
        assert meta["backend"] == "local_wiki"
        assert "bm25_original" in cands[0].branch_ranks
    finally:
        backend.close()
