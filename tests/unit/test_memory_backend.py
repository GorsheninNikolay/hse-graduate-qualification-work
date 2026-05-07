"""Unit tests for framework.cache.memory_backend.MemoryBackend.

Fully in-process - no docker-compose, no skip. Uses real cachetools.TTLCache
so TTL expiry is exercised end-to-end without mocking.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest_asyncio

from framework.cache.backend import CacheCounters
from framework.cache.memory_backend import MemoryBackend
from framework.dsl.schema import CacheProfile, InvalidationConfig


def _make_profile(ttl_seconds: int = 60, max_entries: int = 100) -> CacheProfile:
    return CacheProfile(
        backend="in_memory",
        eviction="lru",
        ttl_seconds=ttl_seconds,
        max_entries=max_entries,
        invalidation=InvalidationConfig(double_delete_delay_ms=0),
    )


@pytest_asyncio.fixture
async def backend() -> AsyncIterator[tuple[MemoryBackend, CacheCounters]]:
    """Yield (MemoryBackend, CacheCounters) wired with a single 'hot' profile."""
    profiles = {"hot": _make_profile(ttl_seconds=60, max_entries=100)}
    counters = CacheCounters()
    yield MemoryBackend(profiles, counters), counters


@pytest_asyncio.fixture
async def short_ttl_backend() -> AsyncIterator[tuple[MemoryBackend, CacheCounters]]:
    """Same as `backend` but with ttl_seconds=1 for the TTL-expiry test."""
    profiles = {"hot": _make_profile(ttl_seconds=1, max_entries=100)}
    counters = CacheCounters()
    yield MemoryBackend(profiles, counters), counters


async def test_get_returns_none_on_miss(
    backend: tuple[MemoryBackend, CacheCounters],
) -> None:
    b, counters = backend
    assert await b.get("nope") is None
    assert counters.misses == 1
    assert counters.hits == 0


async def test_set_with_indexes_then_get_hit(
    backend: tuple[MemoryBackend, CacheCounters],
) -> None:
    b, counters = backend
    await b.set_with_indexes(
        "hot", "k1", b"v1", ttl=60, rule="r1", tags=["t1"]
    )
    assert await b.get("k1") == b"v1"
    assert counters.hits == 1
    assert counters.sets == 1
    assert counters.misses == 0


async def test_set_with_indexes_then_del_by_rule_clears(
    backend: tuple[MemoryBackend, CacheCounters],
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
    backend: tuple[MemoryBackend, CacheCounters],
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
    backend: tuple[MemoryBackend, CacheCounters],
) -> None:
    b, _counters = backend
    await b.set_with_indexes(
        "hot", "k", b"v", ttl=60, rule="r1", tags=["t1"]
    )
    assert await b.get("k") == b"v"
    await b.delete("k")
    assert await b.get("k") is None


async def test_ttl_expiry(
    short_ttl_backend: tuple[MemoryBackend, CacheCounters],
) -> None:
    b, _counters = short_ttl_backend
    await b.set_with_indexes(
        "hot", "k", b"v", ttl=1, rule="r1", tags=["t1"]
    )
    # cachetools.TTLCache evicts on access; sleep past the per-profile TTL.
    await asyncio.sleep(1.5)
    assert await b.get("k") is None


async def test_flush_clears_everything(
    backend: tuple[MemoryBackend, CacheCounters],
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
