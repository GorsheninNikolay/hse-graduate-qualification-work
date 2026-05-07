"""In-memory CacheBackend implementation backed by per-profile cachetools.TTLCache.

Concurrency discipline (ADR-024): one asyncio.Lock guards ALL mutations
across storage caches and the rule/tag indexes. Reads (`get`) are also
under the lock to prevent racing against an evicting `del_by_*`.

Per-profile TTLCache (Q1 from the Phase 2 plan): one cachetools.TTLCache
per profile name, sized to profile.max_entries with ttl=profile.ttl_seconds.
Profiles with ttl_seconds=0 ("no TTL") get math.inf for the TTL parameter
since cachetools requires a finite-or-inf positive number.
"""

import asyncio
import logging
import math
from collections.abc import Mapping, Sequence

import cachetools  # type: ignore[import-untyped]

from framework.cache.backend import CacheCounters
from framework.dsl.schema import CacheProfile

logger = logging.getLogger(__name__)


class MemoryBackend:
    """Implements framework.cache.backend.CacheBackend with per-profile TTLCache.

    Conformance is structural via runtime_checkable Protocol.

    The constructor takes only the in-memory profiles - profiles whose
    backend is "redis" are ignored (the framework selects the backend per
    profile in cli.py / server.py wiring; this class never sees redis profiles).
    """

    def __init__(
        self,
        profiles: Mapping[str, CacheProfile],
        counters: CacheCounters,
    ) -> None:
        in_memory_profiles = {
            name: prof for name, prof in profiles.items() if prof.backend == "in_memory"
        }
        self._caches: dict[str, cachetools.TTLCache[str, bytes]] = {}
        for name, prof in in_memory_profiles.items():
            if prof.max_entries is None:
                # Loader (Phase 1) enforces max_entries required for in_memory
                # via Step 6; if we got here without it, surface loudly.
                raise ValueError(
                    f"in_memory profile '{name}' missing max_entries (loader bug?)"
                )
            ttl: float = prof.ttl_seconds if prof.ttl_seconds > 0 else math.inf
            self._caches[name] = cachetools.TTLCache(
                maxsize=prof.max_entries, ttl=ttl
            )
        # Reverse index: key -> profile_name. Needed for del_by_rule/tag to
        # know which profile cache to evict from.
        self._key_profile: dict[str, str] = {}
        # Forward indexes: rule/tag -> set[key].
        self._rule_index: dict[str, set[str]] = {}
        self._tag_index: dict[str, set[str]] = {}
        self._lock = asyncio.Lock()
        self._counters = counters

    # --- CacheBackend Protocol methods -------------------------------------

    async def get(self, key: str) -> bytes | None:
        async with self._lock:
            profile_name = self._key_profile.get(key)
            if profile_name is None:
                self._counters.misses += 1
                return None
            cache = self._caches[profile_name]
            value: bytes | None = cache.get(key)
            if value is None:
                # TTL expired or evicted between key_profile add and now.
                # Clean up the dangling reverse index.
                self._key_profile.pop(key, None)
                self._counters.misses += 1
                return None
            self._counters.hits += 1
            return value

    async def set(self, key: str, value: bytes, ttl: int) -> None:
        # The plain `set` lacks profile context; it is the ad-hoc / test path
        # only. Production code uses set_with_indexes. We arbitrarily pick the
        # first available profile cache so tests can use this method without
        # plumbing profile_name; if no in-memory profiles exist, raise.
        async with self._lock:
            if not self._caches:
                raise RuntimeError(
                    "MemoryBackend.set requires at least one in_memory profile; "
                    "the registry has none. Use set_with_indexes with a profile_name."
                )
            profile_name = next(iter(self._caches))
            cache = self._caches[profile_name]
            cache[key] = value
            self._key_profile[key] = profile_name
            self._counters.sets += 1
        _ = ttl  # ttl is governed by the per-profile TTLCache, not per-call

    async def set_with_indexes(
        self,
        profile_name: str,
        key: str,
        value: bytes,
        ttl: int,
        rule: str,
        tags: Sequence[str],
    ) -> None:
        async with self._lock:
            cache = self._caches.get(profile_name)
            if cache is None:
                raise KeyError(
                    f"MemoryBackend has no cache for profile '{profile_name}'"
                )
            cache[key] = value
            self._key_profile[key] = profile_name
            self._rule_index.setdefault(rule, set()).add(key)
            for tag in tags:
                self._tag_index.setdefault(tag, set()).add(key)
            self._counters.sets += 1
        _ = ttl  # per-profile TTLCache, not per-call

    async def delete(self, key: str) -> None:
        async with self._lock:
            profile_name = self._key_profile.pop(key, None)
            if profile_name is not None:
                self._caches[profile_name].pop(key, None)

    async def del_by_rule(self, rule: str) -> None:
        async with self._lock:
            keys = self._rule_index.pop(rule, set())
            for key in keys:
                profile_name = self._key_profile.pop(key, None)
                if profile_name is not None:
                    self._caches[profile_name].pop(key, None)
            # Also remove these keys from any tag indexes that referenced them.
            for tag, tag_keys in list(self._tag_index.items()):
                tag_keys.difference_update(keys)
                if not tag_keys:
                    del self._tag_index[tag]
            self._counters.invalidations += 1

    async def del_by_tag(self, tag: str) -> None:
        async with self._lock:
            keys = self._tag_index.pop(tag, set())
            for key in keys:
                profile_name = self._key_profile.pop(key, None)
                if profile_name is not None:
                    self._caches[profile_name].pop(key, None)
            # Remove these keys from any rule indexes too.
            for rule, rule_keys in list(self._rule_index.items()):
                rule_keys.difference_update(keys)
                if not rule_keys:
                    del self._rule_index[rule]
            self._counters.invalidations += 1

    async def flush(self) -> None:
        async with self._lock:
            for cache in self._caches.values():
                cache.clear()
            self._key_profile.clear()
            self._rule_index.clear()
            self._tag_index.clear()
