"""CacheBackend Protocol and CacheCounters dataclass.

Defines the abstraction that RedisBackend and MemoryBackend implement.
Phase 2's Interceptor (read path) and Invalidator (post-commit) consume
this Protocol; concrete backend selection happens in framework/cli.py
based on which `cacheProfiles.<name>.backend` values appear in the
loaded DSL document (Q8 - conditional Redis instantiation).

Cache key format is fixed: sha256(operation_name + sorted_args_json) per
mvp-roadmap.md line 35. The Protocol is bytes-payload to keep
serialization concerns out of the backend.

Single-delete-after-commit (ADR-021 deferred - known limitation): the
Invalidator calls del_by_rule / del_by_tag exactly once per mutation; no
double-delete window. Tag-set leak (EXPIRE GT, ADR-024) is also deferred.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(slots=True)
class CacheCounters:
    """Per-backend in-process counters. reads these on /stats.

    Mutated under the backend's own concurrency discipline (Redis: pipelined;
    in-memory: under the single asyncio.Lock).
    """

    hits: int = 0
    misses: int = 0
    sets: int = 0
    invalidations: int = 0


@runtime_checkable
class CacheBackend(Protocol):
    """Async cache backend interface.

    Two write methods:
      - set(key, value, ttl): primitive write without index updates. For
        ad-hoc tests; the production read path uses set_with_indexes.
      - set_with_indexes(profile_name, key, value, ttl, rule, tags): the
        production write - also updates rule_index:<rule> and
        tag_index:<tag> for each tag, atomically (Redis: pipelined SADDs;
        in-memory: under the lock).

    Three delete methods:
      - delete(key): single-key removal.
      - del_by_rule(rule): atomic - Redis Lua, in-memory under-lock.
      - del_by_tag(tag): atomic - Redis Lua, in-memory under-lock.

    plus flush() for test/teardown.
    """

    async def get(self, key: str) -> bytes | None:
        """Return cached payload bytes, or None on miss."""
        ...

    async def set(self, key: str, value: bytes, ttl: int) -> None:
        """Write a payload with TTL. ttl=0 means no expiry (per dsl-spec
        line 147). Does NOT update rule/tag indexes - see set_with_indexes."""
        ...

    async def set_with_indexes(
        self,
        profile_name: str,
        key: str,
        value: bytes,
        ttl: int,
        rule: str,
        tags: Sequence[str],
    ) -> None:
        """Production write: payload + rule index + tag indexes, atomically.

        profile_name disambiguates per-profile TTLCache for the in-memory
        backend (Q1 - one TTLCache per profile name). The Redis backend
        ignores it (Redis is one keyspace) but the parameter is in the
        contract for symmetry.
        """
        ...

    async def delete(self, key: str) -> None:
        """Single-key delete. No index cleanup; intended for tests + the
        in-memory backend's per-profile cache eviction path."""
        ...

    async def del_by_rule(self, rule: str) -> None:
        """Atomic: SMEMBERS rule_index:<rule> -> DEL all those keys
        -> DEL rule_index:<rule>. Increments counters.invalidations once
        per call (not once per key)."""
        ...

    async def del_by_tag(self, tag: str) -> None:
        """Atomic: SMEMBERS tag_index:<tag> -> DEL keys -> DEL tag_index:<tag>.
        Increments counters.invalidations once per call."""
        ...

    async def flush(self) -> None:
        """Clear everything: Redis FLUSHDB; in-memory clears all caches +
        indexes under the lock. Test/teardown only."""
        ...
