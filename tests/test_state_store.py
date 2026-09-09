"""P2.1：分布式状态存储（内存实现全量单测 + Redis 集成测试按环境开关）"""

import os
import uuid

import pytest
from redis.asyncio import Redis

from app.config import config
from app.state_store import MemoryStateStore, RedisStateStore

# ---- MemoryStateStore ----


async def test_put_get_roundtrip():
    store = MemoryStateStore()
    await store.put("incident", "inc-1", {"status": "firing"}, score=1.0)
    assert await store.get("incident", "inc-1") == {"status": "firing"}
    assert await store.get("incident", "inc-missing") is None
    # kind 隔离
    await store.put("proposal", "inc-1", {"status": "pending"})
    assert await store.get("proposal", "inc-1") == {"status": "pending"}
    assert await store.get("incident", "inc-1") == {"status": "firing"}


async def test_list_sorted_by_score_desc():
    store = MemoryStateStore()
    await store.put("k", "a", {"id": "a"}, score=1.0)
    await store.put("k", "b", {"id": "b"}, score=3.0)
    await store.put("k", "c", {"id": "c"}, score=2.0)
    assert [item["id"] for item in await store.list("k")] == ["b", "c", "a"]
    assert [item["id"] for item in await store.list("k", limit=2)] == ["b", "c"]


async def test_ttl_expiry_lazy_cleanup():
    store = MemoryStateStore()
    await store.put("k", "old", {"id": "old"}, score=1.0, ttl_seconds=0.05)
    await store.put("k", "new", {"id": "new"}, score=2.0)
    await asyncio_sleep(0.08)
    assert await store.get("k", "old") is None
    # list 触发惰性清理
    assert [item["id"] for item in await store.list("k")] == ["new"]


async def test_delete_and_index():
    store = MemoryStateStore()
    await store.put("k", "a", {"id": "a"})
    await store.set_index("fp", "key-1", "a")
    assert await store.get_index("fp", "key-1") == "a"
    await store.set_index("fp", "key-1", None)
    assert await store.get_index("fp", "key-1") is None
    await store.delete("k", "a")
    assert await store.get("k", "a") is None


async def asyncio_sleep(seconds: float) -> None:
    import asyncio

    await asyncio.sleep(seconds)


async def test_runtime_configure_switches_store():
    from app.state_store import state_store_runtime

    original = state_store_runtime.store
    fresh = MemoryStateStore()
    state_store_runtime.configure(fresh, "memory-test")
    try:
        assert state_store_runtime.store is fresh
    finally:
        state_store_runtime.configure(original, "memory")


# ---- RedisStateStore 集成（需要真实 Redis）----


@pytest.mark.integration
@pytest.mark.skipif(
    os.getenv("AIOPS_RUN_REDIS_INTEGRATION") != "1",
    reason="set AIOPS_RUN_REDIS_INTEGRATION=1 and start reliability-stack.yml",
)
async def test_redis_state_store_shared_across_clients() -> None:
    client_a = Redis.from_url(config.redis_url, decode_responses=True)
    client_b = Redis.from_url(config.redis_url, decode_responses=True)
    prefix = f"test-state:{uuid.uuid4().hex}"
    store_a = RedisStateStore(client_a, prefix)
    store_b = RedisStateStore(client_b, prefix)

    try:
        await store_a.put("incident", "inc-1", {"status": "diagnosed"}, score=2.0)
        await store_a.put("incident", "inc-2", {"status": "firing"}, score=3.0)
        await store_a.put("incident", "inc-3", {"status": "resolved"}, score=1.0, ttl_seconds=60)

        # 另一副本可见且按 score 倒序
        assert [item["incident_id"] if "incident_id" in item else None for item in []] == []
        statuses = [item["status"] for item in await store_b.list("incident")]
        assert statuses == ["firing", "diagnosed", "resolved"]

        # 二级索引跨副本生效
        await store_a.set_index("incident_fp", "HighCPU@host-1", "inc-2")
        assert await store_b.get_index("incident_fp", "HighCPU@host-1") == "inc-2"
        await store_b.set_index("incident_fp", "HighCPU@host-1", None)
        assert await store_a.get_index("incident_fp", "HighCPU@host-1") is None

        # 删除同步可见
        await store_b.delete("incident", "inc-2")
        assert await store_a.get("incident", "inc-2") is None
    finally:
        await client_a.aclose()
        await client_b.aclose()
