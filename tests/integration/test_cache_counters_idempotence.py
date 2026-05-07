"""Phase 2 supplementary integration tests (T11): counter accuracy + idempotence.

Three properties verified end-to-end through the booted Starlette app:

  1. Counter accuracy: two getUser calls with the same args yield exactly
     1 miss + 1 hit + 1 set on the backend's CacheCounters (the read-then-
     warm-cache path).
  2. del_by_rule idempotence: calling del_by_rule for a rule that was
     never populated is a no-op (returns cleanly, doesn't raise) and
     still bumps counters.invalidations - covers the ADR-024 known
     limitation about empty index sets.
  3. del_by_tag after TTL expiry: an entry whose value key has TTL-expired
     before del_by_tag is called still allows del_by_tag to run cleanly
     and bumps counters.invalidations - covers the tag-set-leak limitation
     per mvp-roadmap.md section 6.

Sibling test_cache_combinations.py (T10) covers the 6-cell strategy x
backend matrix; T11 is supplementary coverage and reuses the
`_booted_app` / `_gql` helpers from tests/integration/conftest.py
(option 2 in the T11 brief).
"""

from __future__ import annotations

import asyncio

import pytest

from tests.integration.conftest import (
    REPO_ROOT,
    _booted_app,
    _gql,
    postgres_skip,
    redis_skip,
)

# All three tests need postgres up (lifespan opens an asyncpg pool).
# Tests 1 and 3 additionally need redis; that guard is layered per-test.
pytestmark = postgres_skip


@redis_skip
async def test_two_reads_yield_one_miss_one_hit_one_set() -> None:
    """Two getUser calls with same args -> miss + hit + set, exactly.

    The first call misses the cache, runs the SQL, and writes the row;
    the second call hits the cache and does not write. The set count
    bumps once (first call only); hit and miss bump exactly once each.
    """
    yaml_path = REPO_ROOT / "examples" / "graphql-api-tag-redis.yaml"

    async with _booted_app(yaml_path) as client:
        from framework.cli import _state

        # Flush redis to start from a known empty cache state. Other tests
        # may have left tag_index:* / rule_index:* sets behind.
        backends = _state["backends_by_profile"]
        assert backends is not None, "redis-backed yaml should populate backends"
        any_backend = next(iter(backends.values()))
        await any_backend.flush()

        counters = _state["counters"]
        # Snapshot AFTER flush; flush itself doesn't touch counters but a
        # snapshot keeps the assertions robust against any future change.
        snap_misses = counters.misses
        snap_hits = counters.hits
        snap_sets = counters.sets

        # First read: cache miss -> SQL -> set_with_indexes.
        first = await _gql(client, '{ getUser(id: "5") { id name } }')
        assert first == {"getUser": {"id": "5", "name": "user-5"}}
        # Second read: cache hit, no set.
        second = await _gql(client, '{ getUser(id: "5") { id name } }')
        assert second == {"getUser": {"id": "5", "name": "user-5"}}

        assert counters.misses - snap_misses == 1, (
            f"expected exactly 1 miss, got {counters.misses - snap_misses}"
        )
        assert counters.hits - snap_hits == 1, (
            f"expected exactly 1 hit, got {counters.hits - snap_hits}"
        )
        assert counters.sets - snap_sets == 1, (
            f"expected exactly 1 set, got {counters.sets - snap_sets}"
        )


async def test_del_by_rule_on_empty_index_is_noop() -> None:
    """Calling del_by_rule for a rule that has no keys must not raise.

    Uses the in-memory operation-strategy yaml so the test runs without
    redis. Covers the ADR-024 known limitation: empty rule_index sets are
    a no-op, but counters.invalidations still increments per call (one
    invalidation = one user-visible operation, regardless of how many
    keys were actually evicted).
    """
    yaml_path = REPO_ROOT / "examples" / "graphql-api-operation-memory.yaml"

    async with _booted_app(yaml_path) as client:
        from framework.cli import _state

        backends = _state["backends_by_profile"]
        assert backends is not None, (
            "operation-memory yaml should populate backends_by_profile"
        )
        # Pick any backend; del_by_rule with a never-populated rule name.
        any_backend = next(iter(backends.values()))

        before = _state["counters"].invalidations
        # Should be a no-op; must not raise.
        await any_backend.del_by_rule("rule_that_was_never_populated")
        assert _state["counters"].invalidations == before + 1

        # And subsequent reads still work end-to-end.
        result = await _gql(client, '{ getUser(id: "6") { id name } }')
        assert result == {"getUser": {"id": "6", "name": "user-6"}}


@redis_skip
async def test_del_by_tag_after_ttl_expiry_is_safe() -> None:
    """An entry that TTL-expired before del_by_tag still leaves the call clean.

    Uses examples/graphql-api-ttl-1s-redis.yaml (T10's short-TTL variant).
    If T10 hasn't landed it yet, this test fails loudly with FileNotFoundError;
    that's acceptable signal per the T11 brief — the orchestrator re-runs T11
    after T10 closes.

    What we verify: after the value key TTL-expires, the redis tag-set may
    still exist (tag-set-leak limitation, mvp-roadmap.md section 6). Calling
    del_by_tag on it must run cleanly anyway and still bump
    counters.invalidations.
    """
    short_ttl_yaml = REPO_ROOT / "examples" / "graphql-api-ttl-1s-redis.yaml"
    if not short_ttl_yaml.exists():
        pytest.fail(
            f"missing {short_ttl_yaml}; T10 (vkr-j14.10) owns this fixture - "
            "re-run T11 after T10 lands. Do not author this yaml in T11 "
            "(anti-scope: NO new yaml files)."
        )

    async with _booted_app(short_ttl_yaml) as client:
        from framework.cli import _state

        backends = _state["backends_by_profile"]
        assert backends is not None, (
            "redis-backed yaml should populate backends_by_profile"
        )
        any_backend = next(iter(backends.values()))
        # Flush so leftover indexes from prior tests don't perturb the
        # invalidation counter delta below.
        await any_backend.flush()

        # Populate the cache via a read; this writes tag_index:user:7 and
        # the value key with a ~1s TTL.
        result = await _gql(client, '{ getUser(id: "7") { id name } }')
        assert result == {"getUser": {"id": "7", "name": "user-7"}}

        # Wait past TTL so the value key expires. The tag-set may linger
        # (no EXPIRE on tag_index:* per ADR-024 deferred work).
        await asyncio.sleep(1.5)

        before = _state["counters"].invalidations
        # del_by_tag on a tag whose value keys have already TTL-expired -
        # SMEMBERS may return stale member names that DEL no-ops on; the
        # whole sequence must run cleanly and bump invalidations once.
        await any_backend.del_by_tag("user:7")
        assert _state["counters"].invalidations == before + 1
