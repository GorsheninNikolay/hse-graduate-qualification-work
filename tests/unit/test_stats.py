"""Unit tests for framework.stats.{RequestCounters, build_stats_payload, _derive_backend}."""

from typing import Any

from framework.cache.backend import CacheCounters
from framework.dsl.schema import CacheProfile, InvalidationConfig
from framework.stats import RequestCounters, _derive_backend, build_stats_payload


def _profile(backend: str) -> CacheProfile:
    return CacheProfile(
        backend=backend,  # type: ignore[arg-type]
        eviction="lru",
        ttl_seconds=60,
        max_entries=100 if backend == "in_memory" else None,
        invalidation=InvalidationConfig(double_delete_delay_ms=0),
    )


class _FakeRegistry:
    def __init__(self, profiles: dict[str, CacheProfile]) -> None:
        self.cache_profiles = profiles


def test_request_counters_defaults() -> None:
    rc = RequestCounters()
    assert rc.request_count_by_op == {}
    assert rc.errors == {}


def test_derive_backend_all_redis() -> None:
    assert _derive_backend({"a": _profile("redis"), "b": _profile("redis")}) == "redis"


def test_derive_backend_all_in_memory() -> None:
    assert _derive_backend({"a": _profile("in_memory")}) == "in_memory"


def test_derive_backend_mixed() -> None:
    assert _derive_backend({"a": _profile("redis"), "b": _profile("in_memory")}) == "mixed"


def test_derive_backend_none() -> None:
    assert _derive_backend({}) == "none"


def test_build_stats_payload_full_shape() -> None:
    cc = CacheCounters(hits=10, misses=3, sets=4, invalidations=2)
    rc = RequestCounters()
    rc.request_count_by_op["getUser"] = 5
    rc.errors["multiplicity.violation"] = 1
    reg: Any = _FakeRegistry({"hot": _profile("redis")})

    payload = build_stats_payload(cc, rc, reg)

    assert payload == {
        "cache_hits": 10,
        "cache_misses": 3,
        "invalidations": 2,
        "errors": {"multiplicity.violation": 1},
        "request_count_by_op": {"getUser": 5},
        "backend": "redis",
    }


def test_build_stats_payload_deep_copies_dicts() -> None:
    cc = CacheCounters()
    rc = RequestCounters()
    rc.request_count_by_op["getUser"] = 1
    reg: Any = _FakeRegistry({})

    payload = build_stats_payload(cc, rc, reg)
    rc.request_count_by_op["getUser"] = 999
    rc.errors["new"] = 7

    assert payload["request_count_by_op"] == {"getUser": 1}
    assert payload["errors"] == {}
