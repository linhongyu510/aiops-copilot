"""基于 SQLite FTS5 + BM25 的本地 Wiki 存储与检索。

仅使用 Python 标准库（sqlite3 / json / dataclasses / pathlib / datetime）。
原始内容保存在 pages / sections 表，检索用的分词后文本写入 FTS5 虚拟表，
通过 ``bm25()`` 排序。所有写操作均幂等（先删后插）。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from .tokenizer import tokenize, tokenize_for_fts

__all__ = ["WikiSection", "WikiPage", "SearchHit", "WikiStore"]


# --------------------------------------------------------------------------- #
# 数据模型
# --------------------------------------------------------------------------- #
@dataclass
class WikiSection:
    """Wiki 页面中的一个二级章节。"""

    heading: str
    content: str
    seq: int


@dataclass
class WikiPage:
    """一篇编译后的 Wiki 页面。"""

    id: str
    source: str
    title: str
    category: str
    content: str
    sections: list[WikiSection] = field(default_factory=list)
    related_pages: list[str] = field(default_factory=list)
    content_hash: str = ""


@dataclass
class SearchHit:
    """一次 BM25 检索命中。

    ``score`` 为转换后的正相似度分（越大越相关）；``rank`` 为命中列表中的
    0 基序号。
    """

    page_id: str
    source: str
    title: str
    section_heading: str
    content_snippet: str
    score: float
    rank: int
    section_seq: int = 0


# --------------------------------------------------------------------------- #
# 存储
# --------------------------------------------------------------------------- #
# FTS5 列顺序: page_id, title, section_heading, content, source
# 对应 bm25 列权重: 页面 id 不参与排序, 标题权重最高, 其次章节标题, 正文次之。
_BM25_WEIGHTS = (0.0, 5.0, 3.0, 1.0, 0.5)


class WikiStore:
    """SQLite FTS5 Wiki 存储。

    参数:
        db_path: SQLite 数据库文件路径；不存在时自动创建父目录。
    """

    def __init__(self, db_path: str):
        self.db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        # hybrid_retrieval_service 通过 asyncio.to_thread 把 bm25_search 调度到
        # 工作线程执行，因此必须允许跨线程使用连接；SQLite 自身串行化访问。
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._setup()

    # ------------------------------------------------------------------ #
    # schema
    # ------------------------------------------------------------------ #
    def _setup(self) -> None:
        """建表并启用外键约束。"""
        c = self._conn
        c.execute("PRAGMA foreign_keys = ON")
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS pages(
                id TEXT PRIMARY KEY,
                source TEXT,
                title TEXT,
                category TEXT,
                content TEXT,
                related_pages TEXT,
                compiled_at TEXT,
                content_hash TEXT
            )
            """
        )
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS sections(
                id TEXT PRIMARY KEY,
                page_id TEXT,
                heading TEXT,
                content TEXT,
                seq INTEGER
            )
            """
        )
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS meta(
                key TEXT PRIMARY KEY,
                value TEXT
            )
            """
        )
        # 检索用虚拟表：插入前文本已用本包分词器预处理为空格分隔 token。
        c.execute(
            """
            CREATE VIRTUAL TABLE IF NOT EXISTS pages_fts USING fts5(
                page_id,
                title,
                section_heading,
                content,
                source,
                tokenize = 'unicode61'
            )
            """
        )
        c.commit()

    # ------------------------------------------------------------------ #
    # meta
    # ------------------------------------------------------------------ #
    def get_meta(self, key: str, default: str | None = None) -> str | None:
        row = self._conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def set_meta(self, key: str, value: str) -> None:
        self._conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)", (key, value))
        self._conn.commit()

    # ------------------------------------------------------------------ #
    # 写入
    # ------------------------------------------------------------------ #
    def upsert_page(self, page: WikiPage) -> None:
        """幂等地写入页面：先清除该页旧数据，再插入 pages / sections / FTS。"""
        conn = self._conn
        with conn:
            # 旧 FTS 行按 rowid 删除（FTS5 仅支持 rowid 过滤删除）。
            fts_rows = conn.execute(
                "SELECT rowid FROM pages_fts WHERE page_id = ?", (page.id,)
            ).fetchall()
            for r in fts_rows:
                conn.execute("DELETE FROM pages_fts WHERE rowid = ?", (r["rowid"],))
            conn.execute("DELETE FROM sections WHERE page_id = ?", (page.id,))

            compiled_at = datetime.now(UTC).isoformat()
            conn.execute(
                """
                INSERT OR REPLACE INTO pages
                    (id, source, title, category, content, related_pages,
                     compiled_at, content_hash)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    page.id,
                    page.source,
                    page.title,
                    page.category,
                    page.content,
                    json.dumps(page.related_pages, ensure_ascii=False),
                    compiled_at,
                    page.content_hash,
                ),
            )

            title_fts = tokenize_for_fts(page.title)
            source_fts = tokenize_for_fts(page.source)
            for sec in page.sections:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO sections(id, page_id, heading, content, seq)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (f"{page.id}#{sec.seq}", page.id, sec.heading, sec.content, sec.seq),
                )
                conn.execute(
                    """
                    INSERT INTO pages_fts(page_id, title, section_heading, content, source)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        page.id,
                        title_fts,
                        tokenize_for_fts(sec.heading),
                        tokenize_for_fts(sec.content),
                        source_fts,
                    ),
                )

    def delete_page(self, page_id: str) -> None:
        """级联删除页面、其章节及对应 FTS 行。"""
        conn = self._conn
        with conn:
            fts_rows = conn.execute(
                "SELECT rowid FROM pages_fts WHERE page_id = ?", (page_id,)
            ).fetchall()
            for r in fts_rows:
                conn.execute("DELETE FROM pages_fts WHERE rowid = ?", (r["rowid"],))
            conn.execute("DELETE FROM sections WHERE page_id = ?", (page_id,))
            conn.execute("DELETE FROM pages WHERE id = ?", (page_id,))

    # ------------------------------------------------------------------ #
    # 检索
    # ------------------------------------------------------------------ #
    @staticmethod
    def _build_snippet(content: str, query_tokens: list[str], width: int = 160) -> str:
        """从原始章节内容中截取围绕首个命中 token 的片段。"""
        if not content:
            return ""
        lowered = content.lower()
        pos = -1
        for tok in query_tokens:
            p = lowered.find(tok.lower())
            if p != -1:
                pos = p
                break
        if pos == -1:
            return content[:width].strip()
        start = max(0, pos - width // 2)
        end = min(len(content), start + width)
        start = max(0, end - width)
        left = "…" if start > 0 else ""
        right = "…" if end < len(content) else ""
        return f"{left}{content[start:end].strip()}{right}"

    def search(self, query: str, top_k: int = 20) -> list[SearchHit]:
        """BM25 检索。

        参数:
            query: 原始查询文本。
            top_k: 返回上限。

        返回:
            按相关度降序排列的命中列表；查询为空或无命中时返回空列表。
        """
        tokens = tokenize(query)
        if not tokens:
            return []
        # OR 连接以最大化召回，再由 bm25 排序。
        match_expr = " OR ".join(tokens)

        sql = (
            "SELECT p.id AS page_id, p.source AS source, p.title AS title, "
            "       pages_fts.section_heading AS section_heading, "
            f"       bm25(pages_fts, {', '.join(str(w) for w in _BM25_WEIGHTS)}) AS rank "
            "FROM pages_fts JOIN pages p ON p.id = pages_fts.page_id "
            "WHERE pages_fts MATCH ? "
            "ORDER BY rank ASC LIMIT ?"
        )
        try:
            rows = self._conn.execute(sql, (match_expr, top_k)).fetchall()
        except sqlite3.Error:
            return []

        hits: list[SearchHit] = []
        for idx, row in enumerate(rows):
            # FTS 中存的是分词后的 section_heading，这里据此反查原始章节。
            sec = self._resolve_section(row["page_id"], row["section_heading"])
            seq = sec["seq"] if sec else 0
            sec_content = sec["content"] if sec else ""
            raw_heading = sec["heading"] if sec else row["section_heading"]
            # bm25 值越小越相关；取负号转为正相似度分。
            score = -float(row["rank"])
            hits.append(
                SearchHit(
                    page_id=row["page_id"],
                    source=row["source"],
                    title=row["title"],
                    section_heading=raw_heading,
                    content_snippet=self._build_snippet(sec_content, tokens),
                    score=score,
                    rank=idx,
                    section_seq=seq,
                )
            )
        return hits

    def _resolve_section(self, page_id: str, fts_heading: str) -> sqlite3.Row | None:
        """按分词后 heading 反查原始章节（确定性：首个匹配）。"""
        sec_rows = self._conn.execute(
            "SELECT heading, content, seq FROM sections WHERE page_id = ? ORDER BY seq",
            (page_id,),
        ).fetchall()
        for s in sec_rows:
            if tokenize_for_fts(s["heading"]) == fts_heading:
                return s
        # 兜底：无匹配时返回首个章节（保持确定性）。
        return sec_rows[0] if sec_rows else None

    # ------------------------------------------------------------------ #
    # 读取
    # ------------------------------------------------------------------ #
    def get_page(self, page_id: str) -> WikiPage | None:
        row = self._conn.execute("SELECT * FROM pages WHERE id = ?", (page_id,)).fetchone()
        if not row:
            return None
        sec_rows = self._conn.execute(
            "SELECT heading, content, seq FROM sections WHERE page_id = ? ORDER BY seq",
            (page_id,),
        ).fetchall()
        return WikiPage(
            id=row["id"],
            source=row["source"],
            title=row["title"],
            category=row["category"],
            content=row["content"],
            sections=[
                WikiSection(heading=s["heading"], content=s["content"], seq=s["seq"])
                for s in sec_rows
            ],
            related_pages=json.loads(row["related_pages"] or "[]"),
            content_hash=row["content_hash"] or "",
        )

    def list_pages(self) -> list[WikiPage]:
        rows = self._conn.execute("SELECT id FROM pages ORDER BY id").fetchall()
        pages: list[WikiPage] = []
        for r in rows:
            page = self.get_page(r["id"])
            if page is not None:
                pages.append(page)
        return pages

    def page_count(self) -> int:
        row = self._conn.execute("SELECT COUNT(*) AS n FROM pages").fetchone()
        return int(row["n"]) if row else 0

    def close(self) -> None:
        """关闭数据库连接。"""
        self._conn.close()
