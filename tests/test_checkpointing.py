from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from langgraph.checkpoint.memory import MemorySaver

from app.checkpointing import CheckpointRuntime
from app.config import config


@pytest.mark.asyncio
async def test_memory_checkpoint_runtime(monkeypatch) -> None:
    monkeypatch.setattr(config, "checkpoint_backend", "memory")
    runtime = CheckpointRuntime()
    assert isinstance(await runtime.start(), MemorySaver)
    await runtime.close()


@pytest.mark.asyncio
async def test_checkpoint_runtime_rejects_unknown_backend(monkeypatch) -> None:
    monkeypatch.setattr(config, "checkpoint_backend", "unknown")
    with pytest.raises(ValueError, match="unsupported"):
        await CheckpointRuntime().start()


@pytest.mark.asyncio
async def test_checkpoint_runtime_falls_back_when_dsn_missing(monkeypatch) -> None:
    monkeypatch.setattr(config, "checkpoint_backend", "postgres")
    monkeypatch.setattr(config, "checkpoint_postgres_dsn", "")
    monkeypatch.setattr(config, "checkpoint_required", False)
    runtime = CheckpointRuntime()
    assert isinstance(await runtime.start(), MemorySaver)
    assert runtime.degraded_reason


@pytest.mark.asyncio
async def test_checkpoint_runtime_uses_postgres_context(monkeypatch) -> None:
    class Context:
        def __init__(self):
            self.saver = SimpleNamespace(setup=AsyncMock())
            self.closed = False

        async def __aenter__(self):
            return self.saver

        async def __aexit__(self, *_args):
            self.closed = True

    context = Context()
    monkeypatch.setattr(config, "checkpoint_backend", "postgres")
    monkeypatch.setattr(config, "checkpoint_postgres_dsn", "postgresql://fixture")
    monkeypatch.setattr(config, "checkpoint_required", True)
    monkeypatch.setattr(
        "langgraph.checkpoint.postgres.aio.AsyncPostgresSaver.from_conn_string",
        lambda _dsn: context,
    )
    runtime = CheckpointRuntime()
    assert await runtime.start() is context.saver
    assert runtime.backend == "postgres"
    context.saver.setup.assert_awaited_once()
    await runtime.close()
    assert context.closed is True
