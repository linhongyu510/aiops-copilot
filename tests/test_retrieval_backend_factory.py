"""检索后端抽象层单例工厂测试。"""

from __future__ import annotations

from app.config import config
from app.services import retrieval_backend
from app.services.local_wiki import LocalWikiBackend
from app.services.retrieval_backend import MilvusBackend


def test_default_backend_is_local_wiki(monkeypatch) -> None:
    monkeypatch.setattr(config, "retrieval_backend", "local_wiki")
    retrieval_backend.reset_retrieval_backend()
    try:
        backend = retrieval_backend.get_retrieval_backend()
        assert backend.backend_type == "local_wiki"
        assert isinstance(backend, LocalWikiBackend)
    finally:
        retrieval_backend.reset_retrieval_backend()


def test_milvus_backend_selection(monkeypatch) -> None:
    monkeypatch.setattr(config, "retrieval_backend", "milvus")
    retrieval_backend.reset_retrieval_backend()
    try:
        backend = retrieval_backend.get_retrieval_backend()
        assert isinstance(backend, MilvusBackend)
        assert backend.backend_type == "milvus"
        assert backend.supports_dense is True
    finally:
        retrieval_backend.reset_retrieval_backend()


def test_singleton() -> None:
    retrieval_backend.reset_retrieval_backend()
    try:
        first = retrieval_backend.get_retrieval_backend()
        second = retrieval_backend.get_retrieval_backend()
        assert first is second
    finally:
        retrieval_backend.reset_retrieval_backend()


def test_reset_creates_new_instance() -> None:
    retrieval_backend.reset_retrieval_backend()
    first = retrieval_backend.get_retrieval_backend()
    retrieval_backend.reset_retrieval_backend()
    try:
        second = retrieval_backend.get_retrieval_backend()
        assert first is not second
    finally:
        retrieval_backend.reset_retrieval_backend()


def test_invalid_backend_falls_back_to_local_wiki(monkeypatch) -> None:
    monkeypatch.setattr(config, "retrieval_backend", "invalid_value")
    retrieval_backend.reset_retrieval_backend()
    try:
        backend = retrieval_backend.get_retrieval_backend()
        assert backend.backend_type == "local_wiki"
    finally:
        retrieval_backend.reset_retrieval_backend()
