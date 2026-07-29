"""Lifecycle-managed LangGraph checkpoint backends."""

from __future__ import annotations

from typing import Any

from langgraph.checkpoint.memory import MemorySaver
from loguru import logger

from app.config import config


class CheckpointRuntime:
    """Own a process-lifetime checkpointer and its database connection."""

    def __init__(self) -> None:
        self.backend = "memory"
        self.checkpointer: Any = MemorySaver()
        self._context: Any = None
        self._context_entered = False
        self.degraded_reason: str | None = None

    async def start(self) -> Any:
        selected = config.checkpoint_backend.strip().lower()
        if selected in {"", "memory"}:
            return self.checkpointer
        if selected != "postgres":
            raise ValueError(f"unsupported checkpoint backend: {selected}")
        if not config.checkpoint_postgres_dsn:
            error = RuntimeError("AIOPS_CHECKPOINT_POSTGRES_DSN is required")
            if config.checkpoint_required:
                raise error
            self.degraded_reason = str(error)
            logger.warning(f"Checkpoint 降级为内存: {error}")
            return self.checkpointer

        try:
            from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

            self._context = AsyncPostgresSaver.from_conn_string(
                config.checkpoint_postgres_dsn
            )
            self.checkpointer = await self._context.__aenter__()
            self._context_entered = True
            await self.checkpointer.setup()
            self.backend = "postgres"
            self.degraded_reason = None
            logger.info("PostgreSQL LangGraph checkpoint 已就绪")
        except Exception as exc:
            if self._context is not None and self._context_entered:
                await self._context.__aexit__(type(exc), exc, exc.__traceback__)
            self._context = None
            self._context_entered = False
            self.checkpointer = MemorySaver()
            self.backend = "memory"
            self.degraded_reason = f"{type(exc).__name__}: {exc}"
            if config.checkpoint_required:
                raise
            logger.warning(f"Checkpoint 连接失败，降级为内存: {type(exc).__name__}")
        return self.checkpointer

    async def close(self) -> None:
        if self._context is not None and self._context_entered:
            await self._context.__aexit__(None, None, None)
            self._context = None
            self._context_entered = False


checkpoint_runtime = CheckpointRuntime()
