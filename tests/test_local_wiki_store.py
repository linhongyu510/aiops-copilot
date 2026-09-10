"""WikiStore（SQLite FTS5/BM25）单元测试，使用临时目录，不依赖 aiops-docs。"""

from __future__ import annotations

from pathlib import Path

from app.services.local_wiki.wiki_store import WikiPage, WikiSection, WikiStore


def _make_page(
    page_id: str,
    title: str,
    section_heading: str,
    body: str,
    source: str = "doc.md",
) -> WikiPage:
    """构造一个带单个章节的 WikiPage（FTS 行仅由章节产生）。"""
    return WikiPage(
        id=page_id,
        source=source,
        title=title,
        category="general",
        content=f"# {title}\n## {section_heading}\n{body}",
        sections=[WikiSection(heading=section_heading, content=body, seq=0)],
        related_pages=[],
        content_hash="abc",
    )


def test_create_store_and_empty_count(tmp_path: Path) -> None:
    store = WikiStore(str(tmp_path / "wiki.db"))
    try:
        assert store.page_count() == 0
    finally:
        store.close()


def test_upsert_page_and_retrieve(tmp_path: Path) -> None:
    store = WikiStore(str(tmp_path / "wiki.db"))
    try:
        page = _make_page("p1", "CPU使用率过高", "问题描述", "CPU 持续超过 80%")
        store.upsert_page(page)
        loaded = store.get_page("p1")
        assert loaded is not None
        assert loaded.title == "CPU使用率过高"
        assert loaded.source == "doc.md"
        assert loaded.sections[0].heading == "问题描述"
        assert store.page_count() == 1
    finally:
        store.close()


def test_upsert_page_idempotent(tmp_path: Path) -> None:
    store = WikiStore(str(tmp_path / "wiki.db"))
    try:
        page = _make_page("p1", "标题", "章节", "正文内容")
        store.upsert_page(page)
        store.upsert_page(page)
        store.upsert_page(page)
        assert store.page_count() == 1
    finally:
        store.close()


def test_delete_page(tmp_path: Path) -> None:
    store = WikiStore(str(tmp_path / "wiki.db"))
    try:
        store.upsert_page(_make_page("p1", "标题", "章节", "正文"))
        assert store.get_page("p1") is not None
        store.delete_page("p1")
        assert store.get_page("p1") is None
        assert store.page_count() == 0
    finally:
        store.close()


def test_search_returns_hits(tmp_path: Path) -> None:
    store = WikiStore(str(tmp_path / "wiki.db"))
    try:
        store.upsert_page(
            _make_page("cpu", "CPU使用率过高", "问题描述", "CPU 使用率过高，响应变慢")
        )
        hits = store.search("CPU 使用率", top_k=5)
        assert len(hits) >= 1
        assert hits[0].page_id == "cpu"
        assert hits[0].score > 0
    finally:
        store.close()


def test_search_ranking(tmp_path: Path) -> None:
    store = WikiStore(str(tmp_path / "wiki.db"))
    try:
        store.upsert_page(_make_page("a", "CPU使用率过高排查", "问题描述", "CPU 使用率持续超阈值"))
        store.upsert_page(
            _make_page("b", "磁盘空间清理", "操作步骤", "清理日志，偶尔伴随 CPU 波动")
        )
        hits = store.search("CPU 使用率", top_k=5)
        assert hits, "应当命中至少一条"
        # 标题含查询词的页面标题权重最高，应排在最前。
        assert hits[0].page_id == "a"
        # rank 为 0 基序号且单调递增。
        assert [h.rank for h in hits] == list(range(len(hits)))
    finally:
        store.close()


def test_search_top_k_limit(tmp_path: Path) -> None:
    store = WikiStore(str(tmp_path / "wiki.db"))
    try:
        for i in range(3):
            store.upsert_page(_make_page(f"p{i}", f"通用标题 {i}", "章节", "通用关键词正文内容"))
        hits = store.search("通用", top_k=2)
        assert len(hits) == 2
    finally:
        store.close()


def test_search_empty_query(tmp_path: Path) -> None:
    store = WikiStore(str(tmp_path / "wiki.db"))
    try:
        store.upsert_page(_make_page("p1", "标题", "章节", "正文"))
        assert store.search("", top_k=5) == []
    finally:
        store.close()


def test_list_pages(tmp_path: Path) -> None:
    store = WikiStore(str(tmp_path / "wiki.db"))
    try:
        for pid in ("p1", "p2", "p3"):
            store.upsert_page(_make_page(pid, f"标题{pid}", "章节", "正文"))
        pages = store.list_pages()
        assert {p.id for p in pages} == {"p1", "p2", "p3"}
    finally:
        store.close()


def test_close_and_reopen(tmp_path: Path) -> None:
    db = str(tmp_path / "wiki.db")
    store = WikiStore(db)
    store.upsert_page(_make_page("p1", "标题", "章节", "持久化正文"))
    store.close()

    reopened = WikiStore(db)
    try:
        page = reopened.get_page("p1")
        assert page is not None
        assert page.title == "标题"
        assert reopened.page_count() == 1
    finally:
        reopened.close()
