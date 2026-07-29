from pathlib import Path

import pytest

from app.config import config
from app.services.document_splitter_service import DocumentSplitterService
from app.services.vector_index_service import VectorIndexService


def test_plain_chunks_respect_configured_max_size() -> None:
    splitter = DocumentSplitterService()
    content = ("CPU 使用率持续过高，需要检查进程负载和慢查询。\n" * 200).strip()

    documents = splitter.split_text(content, "cpu.md")

    assert len(documents) > 1
    assert max(len(document.page_content) for document in documents) <= config.chunk_max_size


def test_index_directory_is_restricted_to_upload_root(tmp_path: Path) -> None:
    service = VectorIndexService()
    upload_root = tmp_path / "uploads"
    upload_root.mkdir()
    service.upload_path = str(upload_root)

    assert service.resolve_index_directory() == upload_root.resolve()
    with pytest.raises(ValueError, match="仅允许索引上传目录"):
        service.resolve_index_directory(str(tmp_path.parent))
