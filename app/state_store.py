"""分布式状态存储（P2.1）：incident 与变更提案的 Redis 化。

与 coordination.py 同一套约定：
- 默认单进程内存实现（可离线测试）；
- AIOPS_COORDINATION_BACKEND=redis 时切换 Redis 实现
  （hash-per-entity + ZSET 索引 + 二级索引），由 main lifespan 注入；
- 降级不中断主流程，/production/readiness 如实暴露 backend 状态。

实体为普通 dict（JSON 可序列化），kind 划分命名空间（incident/proposal）。

需要 ``[state]`` extra（``redis``）。缺失时抛
:class:`aiops_core._optional.OptionalDependencyMissing`。
"""

from __future__ import annotations

import json
import time
from typing import Any

from loguru import logger

try:
    from redis.asyncio import Redis
except ImportError as _exc:  # pragma: no cover - depends on install profile
    from aiops_core._optional import OptionalDependencyMissing

    raise OptionalDependencyMissing(
        "app.state_store 需要 `[state]` extra（redis）。"
        "\n    pip install 'aiops-copilot[state]'"
    ) from _exc


class MemoryStateStore:
    """单进程内存实现：dict + 惰性 TTL + 内存索引。"""

    def __init__(self) -> None:
        # kind -> {id: {"data": dict, "score": float, "expires_at": float|None}}
        self._entities: dict[str, dict[str, dict[str, Any]]] = {}
        self._index: dict[str, dict[str, str]] = {}

    @staticmethod
    def _expired(entry: dict[str, Any]) -> bool:
        expires_at = entry.get("expires_at")
        return expires_at is not None and time.time() >= expires_at

    async def put(
        self,
        kind: str,
        entity_id: str,
        data: dict[str, Any],
        *,
        score: float = 0.0,
        ttl_seconds: float | None = None,
    ) -> None:
        bucket = self._entities.setdefault(kind, {})
        bucket[entity_id] = {
            "data": data,
            "score": score,
            "expires_at": (time.time() + ttl_seconds) if ttl_seconds else None,
        }

    async def get(self, kind: str, entity_id: str) -> dict[str, Any] | None:
        entry = self._entities.get(kind, {}).get(entity_id)
        if entry is None:
            return None
        if self._expired(entry):
            bucket = self._entities.get(kind)
            if bucket is not None:
                bucket.pop(entity_id, None)
            return None
        return dict(entry["data"])

    async def delete(self, kind: str, entity_id: str) -> None:
        self._entities.get(kind, {}).pop(entity_id, None)

    async def list(self, kind: str, *, limit: int = 200) -> list[dict[str, Any]]:
        bucket = self._entities.get(kind, {})
        alive = {
            entity_id: entry
            for entity_id, entry in bucket.items()
            if not self._expired(entry)
        }
        # 清理已过期项，防止长期驻留
        if len(alive) != len(bucket):
            self._entities[kind] = alive
        ranked = sorted(alive.items(), key=lambda pair: -pair[1]["score"])
        return [dict(entry["data"]) for _entity_id, entry in ranked[: max(limit, 0)]]

    async def set_index(self, name: str, key: str, entity_id: str | None) -> None:
        mapping = self._index.setdefault(name, {})
        if entity_id is None:
            mapping.pop(key, None)
        else:
            mapping[key] = entity_id

    async def get_index(self, name: str, key: str) -> str | None:
        return self._index.get(name, {}).get(key)


class RedisStateStore:
    """Redis 实现：{prefix}:{kind}:{id} 哈希 JSON + ZSET 时间索引 + 二级索引。"""

    def __init__(self, client: Redis, prefix: str):
        self.client = client
        self.prefix = prefix.strip(":") or "aiops"

    def _entity_key(self, kind: str, entity_id: str) -> str:
        return f"{self.prefix}:state:{kind}:{entity_id}"

    def _zset_key(self, kind: str) -> str:
        return f"{self.prefix}:state:zset:{kind}"

    def _index_key(self, name: str, key: str) -> str:
        return f"{self.prefix}:state:idx:{name}:{key}"

    async def put(
        self,
        kind: str,
        entity_id: str,
        data: dict[str, Any],
        *,
        score: float = 0.0,
        ttl_seconds: float | None = None,
    ) -> None:
        encoded = json.dumps(data, ensure_ascii=False)
        ttl = max(1, int(ttl_seconds)) if ttl_seconds else None
        pipe = self.client.pipeline(transaction=True)
        pipe.set(self._entity_key(kind, entity_id), encoded, ex=ttl)
        pipe.zadd(self._zset_key(kind), {entity_id: score})
        if ttl:
            pipe.expire(self._zset_key(kind), ttl)
        await pipe.execute()

    async def get(self, kind: str, entity_id: str) -> dict[str, Any] | None:
        raw = await self.client.get(self._entity_key(kind, entity_id))
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            logger.warning(f"状态存储坏数据：{kind}/{entity_id}")
            return None

    async def delete(self, kind: str, entity_id: str) -> None:
        pipe = self.client.pipeline(transaction=True)
        pipe.delete(self._entity_key(kind, entity_id))
        pipe.zrem(self._zset_key(kind), entity_id)
        await pipe.execute()

    async def list(self, kind: str, *, limit: int = 200) -> list[dict[str, Any]]:
        ids = await self.client.zrevrange(self._zset_key(kind), 0, max(limit, 0) - 1)
        if not ids:
            return []
        keys = [self._entity_key(kind, entity_id) for entity_id in ids]
        values = await self.client.mget(keys)
        results: list[dict[str, Any]] = []
        # MGET returns exactly one slot per requested key, including misses.
        for entity_id, raw in zip(ids, values, strict=True):
            if raw is None:
                # 实体已过期但仍在 ZSET：惰性清理
                await self.client.zrem(self._zset_key(kind), entity_id)
                continue
            try:
                results.append(json.loads(raw))
            except json.JSONDecodeError:
                logger.warning(f"状态存储坏数据：{kind}/{entity_id}")
        return results

    async def set_index(self, name: str, key: str, entity_id: str | None) -> None:
        index_key = self._index_key(name, key)
        if entity_id is None:
            await self.client.delete(index_key)
        else:
            await self.client.set(index_key, entity_id)

    async def get_index(self, name: str, key: str) -> str | None:
        value = await self.client.get(self._index_key(name, key))
        return str(value) if value is not None else None


class StateStoreRuntime:
    """持有当前生效的状态存储实现；与 coordination 同步切换。"""

    def __init__(self) -> None:
        self.backend = "memory"
        self.store: MemoryStateStore | RedisStateStore = MemoryStateStore()

    def configure(self, store: MemoryStateStore | RedisStateStore, backend: str) -> None:
        self.store = store
        self.backend = backend

    def reset(self) -> None:
        self.configure(MemoryStateStore(), "memory")


state_store_runtime = StateStoreRuntime()
