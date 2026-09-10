"""local_wiki：基于 SQLite FTS5/BM25 的本地 Wiki 检索后端。

零第三方依赖，仅使用 Python 标准库。Markdown Runbook 经编译后存入本地
SQLite，提供确定性中文分词与 BM25 检索。
"""

from .backend import LocalWikiBackend
from .tokenizer import tokenize, tokenize_for_fts
from .wiki_compiler import CompileResult, WikiCompiler
from .wiki_store import SearchHit, WikiPage, WikiSection, WikiStore

__all__ = [
    "LocalWikiBackend",
    "WikiPage",
    "WikiSection",
    "SearchHit",
    "WikiStore",
    "WikiCompiler",
    "CompileResult",
    "tokenize",
    "tokenize_for_fts",
]
