"""Unit tests for framework.cache.redis_backend.RedisBackend.

Tests require `make up` running so redis is reachable on 127.0.0.1:6379.
The whole module SKIPS itself if the port is unreachable, mirroring the
postgres-reachability skip pattern in tests/integration/test_graphql_endpoint.py.

The fixture flushes the database before AND after each test so individual
tests are isolated even though the redis instance is shared with the
docker-compose framework container.
"""

from __future__ import annotations

import asyncio
import socket
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
import redis.asyncio as redis_async

from framework.cache.backend import CacheCounters
from framework.cache.redis_backend import RedisBackend


def _redis_reachable() -> bool:
    try:
        with socket.create_connection(("127.0.0.1", 6379), timeout=1):
            return True
    except OSError:
        return False


pytestmark = pytest.mark.skipif(
    not _redis_reachable(),
    reason="redis not reachable on 127.0.0.1:6379; run `make up` first",
)


@pytest_asyncio.fixture
async def backend() -> AsyncIterator[tuple[RedisBackend, CacheCounters]]:
    """Yield (RedisBackend, CacheCounters) bound to a freshly-flushed DB."""
    client: redis_async.Redis = redis_async.Redis.from_url(
        "redis://127.0.0.1:6379/0", decode_responses=False
    )
    counters = CacheCounters()
    b = RedisBackend(client, counters)
    await b.flush()
    try:
        yield b, counters
    finally:
        await b.flush()
        await client.aclose()


async def test_get_returns_none_on_miss(
    backend: tuple[RedisBackend, CacheCounters],
) -> None:
    b, counters = backend
    assert await b.get("nope") is None
    assert counters.misses == 1
    assert counters.hits == 0


async def test_set_then_get_hit(
    backend: tuple[RedisBackend, CacheCounters],
) -> None:
    b, counters = backend
    await b.set("k1", b"v1", ttl=60)
    assert await b.get("k1") == b"v1"
    assert counters.hits == 1
    assert counters.sets == 1
    assert counters.misses == 0


async def test_set_with_indexes_then_del_by_rule_clears(
    backend: tuple[RedisBackend, CacheCounters],
) -> None:
    b, counters = backend
    await b.set_with_indexes(
        "hot", "k", b"v", ttl=60, rule="r1", tags=["t1"]
    )
    assert await b.get("k") == b"v"
    await b.del_by_rule("r1")
    assert await b.get("k") is None
    assert counters.invalidations == 1


async def test_set_with_indexes_then_del_by_tag_clears(
    backend: tuple[RedisBackend, CacheCounters],
) -> None:
    b, counters = backend
    await b.set_with_indexes(
        "hot", "k", b"v", ttl=60, rule="r1", tags=["t1"]
    )
    assert await b.get("k") == b"v"
    await b.del_by_tag("t1")
    assert await b.get("k") is None
    assert counters.invalidations == 1


async def test_delete_removes_single_key(
    backend: tuple[RedisBackend, CacheCounters],
) -> None:
    b, _counters = backend
    await b.set("k", b"v", ttl=60)
    assert await b.get("k") == b"v"
    await b.delete("k")
    assert await b.get("k") is None


async def test_ttl_expiry(
    backend: tuple[RedisBackend, CacheCounters],
) -> None:
    b, _counters = backend
    await b.set("k", b"v", ttl=1)
    # Use asyncio.sleep so the event loop stays responsive.
    await asyncio.sleep(2)
    assert await b.get("k") is None


async def test_flush_clears_everything(
    backend: tuple[RedisBackend, CacheCounters],
) -> None:
    b, _counters = backend
    await b.set_with_indexes(
        "hot", "k1", b"v1", ttl=60, rule="r1", tags=["t1"]
    )
    await b.set_with_indexes(
        "hot", "k2", b"v2", ttl=60, rule="r2", tags=["t2"]
    )
    await b.flush()
    assert await b.get("k1") is None
    assert await b.get("k2") is None
    # del_by_rule / del_by_tag on now-empty indexes should be no-ops.
    await b.del_by_rule("r1")
    await b.del_by_tag("t2")
    assert await b.get("k1") is None
    assert await b.get("k2") is None
