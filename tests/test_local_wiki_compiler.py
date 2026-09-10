"""WikiCompiler（Markdown -> Wiki 页面）单元测试，使用临时 docs 目录。"""

from __future__ import annotations

from pathlib import Path

from app.services.local_wiki.wiki_compiler import WikiCompiler
from app.services.local_wiki.wiki_store import WikiStore


def _make_compiler(tmp_path: Path) -> tuple[WikiCompiler, WikiStore]:
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    store = WikiStore(str(tmp_path / "wiki.db"))
    return WikiCompiler(store, str(docs_dir)), store


def test_compile_single_file(tmp_path: Path) -> None:
    compiler, store = _make_compiler(tmp_path)
    try:
        md = compiler.docs_dir / "alpha.md"
        md.write_text("# Alpha Title\n## Section One\n正文内容。\n", encoding="utf-8")
        page = compiler.compile_file(md)
        assert page.title == "Alpha Title"
        assert page.id == "alpha"
        assert len(page.sections) == 1
        assert page.sections[0].heading == "Section One"
        assert "正文内容" in page.sections[0].content
    finally:
        store.close()


def test_compile_all_multiple_files(tmp_path: Path) -> None:
    compiler, store = _make_compiler(tmp_path)
    try:
        for name in ("a.md", "b.md", "c.md"):
            (compiler.docs_dir / name).write_text(f"# {name}\n## S\nbody\n", encoding="utf-8")
        result = compiler.compile_all()
        assert result.total_files == 3
        assert result.compiled == 3
        assert result.failed == 0
        assert store.page_count() == 3
    finally:
        store.close()


def test_compile_if_needed_skips_unchanged(tmp_path: Path) -> None:
    compiler, store = _make_compiler(tmp_path)
    try:
        (compiler.docs_dir / "a.md").write_text("# A\n## S\nbody\n", encoding="utf-8")
        first = compiler.compile_if_needed()
        assert first.compiled == 1
        assert first.skipped == 0
        second = compiler.compile_if_needed()
        assert second.compiled == 0
        assert second.skipped == 1
    finally:
        store.close()


def test_compile_if_needed_recompiles_changed(tmp_path: Path) -> None:
    compiler, store = _make_compiler(tmp_path)
    try:
        md = compiler.docs_dir / "a.md"
        md.write_text("# A\n## S\nbody v1\n", encoding="utf-8")
        compiler.compile_if_needed()
        compiler.compile_if_needed()  # 第二次应全部 skipped
        md.write_text("# A\n## S\nbody v2 changed\n", encoding="utf-8")
        third = compiler.compile_if_needed()
        assert third.compiled == 1
        page = store.get_page("a")
        assert page is not None
        assert "body v2" in page.content
    finally:
        store.close()


def test_section_parsing(tmp_path: Path) -> None:
    compiler, store = _make_compiler(tmp_path)
    try:
        md = compiler.docs_dir / "deep.md"
        md.write_text(
            "# Deep\n## Main Section\nintro\n### Sub Heading\nsub body\n",
            encoding="utf-8",
        )
        page = compiler.compile_file(md)
        # ### 三级标题归入当前 ## 章节，不开启新章节。
        assert len(page.sections) == 1
        assert page.sections[0].heading == "Main Section"
        assert "### Sub Heading" in page.sections[0].content
        assert "sub body" in page.sections[0].content
    finally:
        store.close()


def test_code_fence_preserved(tmp_path: Path) -> None:
    compiler, store = _make_compiler(tmp_path)
    try:
        md = compiler.docs_dir / "code.md"
        md.write_text(
            "# Code\n## Sec\n```\n# 这不是标题\nprint('hi')\n```\n正文。\n",
            encoding="utf-8",
        )
        page = compiler.compile_file(md)
        # 围栏内的 "# 这不是标题" 不应成为页面 title。
        assert page.title == "Code"
        assert len(page.sections) == 1
        assert "# 这不是标题" in page.sections[0].content
    finally:
        store.close()


def test_related_pages_computed(tmp_path: Path) -> None:
    compiler, store = _make_compiler(tmp_path)
    try:
        (compiler.docs_dir / "cpu.md").write_text(
            "# CPU 使用率告警\n## 问题描述\nbody\n", encoding="utf-8"
        )
        (compiler.docs_dir / "load.md").write_text(
            "# CPU 负载排查\n## 问题描述\nbody\n", encoding="utf-8"
        )
        compiler.compile_all()
        cpu = store.get_page("cpu")
        load = store.get_page("load")
        assert cpu is not None and load is not None
        # 两页标题共享 cpu / 问题描述 等 token，related_pages 非空。
        assert len(cpu.related_pages) >= 1
        assert len(load.related_pages) >= 1
    finally:
        store.close()


def test_compile_result_fields(tmp_path: Path) -> None:
    compiler, store = _make_compiler(tmp_path)
    try:
        (compiler.docs_dir / "a.md").write_text("# A\n## S\nbody\n", encoding="utf-8")
        result = compiler.compile_all()
        assert result.total_files == 1
        assert result.compiled == 1
        assert result.skipped == 0
        assert result.failed == 0
        assert result.duration_ms >= 0
    finally:
        store.close()
