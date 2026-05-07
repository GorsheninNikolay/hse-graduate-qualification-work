"""Phase 3 request counters + /stats payload assembler.

Phase 3 evolves /stats from the Phase-0 literal {"status":"booting"} payload
to a JSON object exposing in-memory counters per roadmap §4 line 158:

    {
      "cache_hits":          int,
      "cache_misses":        int,
      "invalidations":       int,
      "errors":              dict[str, int],   # keyed by extensions.code
      "request_count_by_op": dict[str, int],
      "backend":             "redis" | "in_memory" | "mixed" | "none"
    }

Cache counters live in framework/cache/backend.py:CacheCounters; this module
adds the request-side counters (per-op invocation count + per-error-code count)
and the assembly function that produces the JSON payload.

Phase 4 reads the resulting JSON pre/post each experiment cell and computes
deltas; it is the schema contract between the framework and the matrix runner.

Anti-scope: NO Prometheus exposition format. NO cardinality cap (Phase 5+).
NO statistical aggregation (Phase 4 owns it).
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from framework.cache.backend import CacheCounters
from framework.dsl.registry import OperationRegistry
from framework.dsl.schema import CacheProfile


@dataclass(slots=True)
class RequestCounters:
    """Per-process request counters. Surfaced on /stats by build_stats_payload.

    Mutated by the resolver wrappers in framework/graphql/server.py (P3-04).
    No locking - Python dict mutations are atomic for primitive keys/values
    under the GIL, and the asyncpg pool serializes connection access anyway.
    """

    request_count_by_op: dict[str, int] = field(default_factory=dict)
    errors: dict[str, int] = field(default_factory=dict)


def _derive_backend(profiles: Mapping[str, CacheProfile]) -> str:
    """Return the backend label for the /stats payload.

    'redis'     - all profiles use redis.
    'in_memory' - all profiles use in_memory.
    'mixed'     - some redis, some in_memory.
    'none'      - no cache profiles (no-cache.yaml baseline cell).
    """
    if not profiles:
        return "none"
    backends = {prof.backend for prof in profiles.values()}
    if backends == {"redis"}:
        return "redis"
    if backends == {"in_memory"}:
        return "in_memory"
    return "mixed"


def build_stats_payload(
    cache: CacheCounters,
    requests: RequestCounters,
    registry: OperationRegistry,
) -> dict[str, Any]:
    """Assemble the /stats JSON payload per roadmap §4 line 158.

    The returned dict is a fresh deep-ish copy of the counter dicts (so the
    caller can JSON-serialize it without worrying about concurrent mutation
    racing the response writer).
    """
    return {
        "cache_hits":          cache.hits,
        "cache_misses":        cache.misses,
        "invalidations":       cache.invalidations,
        "errors":              dict(requests.errors),
        "request_count_by_op": dict(requests.request_count_by_op),
        "backend":             _derive_backend(registry.cache_profiles),
    }
