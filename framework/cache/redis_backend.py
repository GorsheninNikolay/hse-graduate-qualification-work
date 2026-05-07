"""Redis-backed CacheBackend implementation with Lua atomicity (ADR-024).

Tag and rule deletions use a Lua script: SMEMBERS the index set -> DEL
all member keys -> DEL the index set itself. The Lua block is loaded once
at construction via SCRIPT LOAD; subsequent calls use EVALSHA.

Single delete after commit (ADR-021 MVP, double-delete deferred). Per-instance
fingerprint namespacing (ADR-031) is also deferred - keys live in the
default keyspace.
"""

import logging
from collections.abc import Awaitable, Sequence
from typing import cast

import redis.asyncio as redis

from framework.cache.backend import CacheCounters

logger = logging.getLogger(__name__)


# Lua script: atomic SMEMBERS -> DEL keys -> DEL index-set.
# Returns the number of value keys that were actually removed.
_DEL_BY_INDEX_LUA = """
local members = redis.call('SMEMBERS', KEYS[1])
local removed = 0
if #members > 0 then
    removed = redis.call('DEL', unpack(members))
end
redis.call('DEL', KEYS[1])
return removed
"""


def _rule_index_key(rule: str) -> str:
    return f"rule_index:{rule}"


def _tag_index_key(tag: str) -> str:
    return f"tag_index:{tag}"


class RedisBackend:
    """Implements framework.cache.backend.CacheBackend against a redis.asyncio client.

    Conformance is structural via runtime_checkable Protocol; no inheritance.
    """

    def __init__(self, client: redis.Redis, counters: CacheCounters) -> None:
        self._client = client
        self._counters = counters
        self._del_by_index_sha: str | None = None  # populated lazily on first invalidation

    async def _ensure_lua_loaded(self) -> str:
        if self._del_by_index_sha is None:
            sha: str = await self._client.script_load(_DEL_BY_INDEX_LUA)
            self._del_by_index_sha = sha
        return self._del_by_index_sha

    # --- CacheBackend Protocol methods -------------------------------------

    async def get(self, key: str) -> bytes | None:
        value: bytes | None = await self._client.get(key)
        if value is None:
            self._counters.misses += 1
        else:
            self._counters.hits += 1
        return value

    async def set(self, key: str, value: bytes, ttl: int) -> None:
        # ttl=0 means "no TTL" per dsl-spec line 147 -> SET without EX.
        if ttl > 0:
            await self._client.set(key, value, ex=ttl)
        else:
            await self._client.set(key, value)
        self._counters.sets += 1

    async def set_with_indexes(
        self,
        profile_name: str,
        key: str,
        value: bytes,
        ttl: int,
        rule: str,
        tags: Sequence[str],
    ) -> None:
        # profile_name unused for Redis (one keyspace).
        # Pipelined writes for atomicity within Redis's single-threaded model.
        async with self._client.pipeline(transaction=False) as pipe:
            if ttl > 0:
                pipe.set(key, value, ex=ttl)
            else:
                pipe.set(key, value)
            pipe.sadd(_rule_index_key(rule), key)
            for tag in tags:
                pipe.sadd(_tag_index_key(tag), key)
            await pipe.execute()
        self._counters.sets += 1

    async def delete(self, key: str) -> None:
        await self._client.delete(key)

    async def del_by_rule(self, rule: str) -> None:
        sha = await self._ensure_lua_loaded()
        await cast(Awaitable[object], self._client.evalsha(sha, 1, _rule_index_key(rule)))
        self._counters.invalidations += 1

    async def del_by_tag(self, tag: str) -> None:
        sha = await self._ensure_lua_loaded()
        await cast(Awaitable[object], self._client.evalsha(sha, 1, _tag_index_key(tag)))
        self._counters.invalidations += 1

    async def flush(self) -> None:
        await self._client.flushdb()
