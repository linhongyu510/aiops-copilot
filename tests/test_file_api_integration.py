from io import BytesIO
from types import SimpleNamespace

import pytest
from fastapi import HTTPException, UploadFile

import app.api.file as module


@pytest.mark.asyncio
async def test_upload_file_success_and_partial_index(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(module, "UPLOAD_DIR", tmp_path)
    monkeypatch.setattr(module.vector_index_service, "index_single_file", lambda _path: None)
    response = await module.upload_file(
        UploadFile(filename="cpu report.md", file=BytesIO("证据".encode()))
    )
    assert response.status_code == 200
    assert b'"index_status":"succeeded"' in response.body
    assert (tmp_path / "cpu_report.md").exists()

    def broken(_path):
        raise RuntimeError("milvus down")

    monkeypatch.setattr(module.vector_index_service, "index_single_file", broken)
    partial = await module.upload_file(
        UploadFile(filename="fallback.txt", file=BytesIO(b"runbook"))
    )
    assert b'"message":"partial_success"' in partial.body


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("filename", "content", "detail"),
    [
        ("bad.exe", b"x", "不支持的文件格式"),
        ("bad.md", b"\xff", "UTF-8"),
    ],
)
async def test_upload_rejects_invalid_input(
    monkeypatch, tmp_path, filename, content, detail
) -> None:
    monkeypatch.setattr(module, "UPLOAD_DIR", tmp_path)
    with pytest.raises(HTTPException, match=detail):
        await module.upload_file(UploadFile(filename=filename, file=BytesIO(content)))


@pytest.mark.asyncio
async def test_upload_rejects_oversized_file(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(module, "UPLOAD_DIR", tmp_path)
    monkeypatch.setattr(module, "MAX_FILE_SIZE", 3)
    with pytest.raises(HTTPException, match="文件大小超过限制"):
        await module.upload_file(
            UploadFile(filename="large.md", file=BytesIO(b"1234"))
        )


@pytest.mark.asyncio
async def test_index_directory_success_and_failure(monkeypatch) -> None:
    result = SimpleNamespace(success=True, to_dict=lambda: {"indexed": 2})
    monkeypatch.setattr(module.vector_index_service, "index_directory", lambda _path: result)
    response = await module.index_directory("uploads")
    assert b'"indexed":2' in response.body

    def broken(_path):
        raise RuntimeError("index failed")

    monkeypatch.setattr(module.vector_index_service, "index_directory", broken)
    with pytest.raises(HTTPException, match="索引目录失败"):
        await module.index_directory("uploads")
