"""Cache-aside read-path interceptor (Phase 2).

Wraps a Phase-1 query resolver with a cache lookup. On hit, deserializes the
cached bytes payload and returns it. On miss, calls the inner resolver,
serializes the result, calls backend.set_with_indexes (so rule/tag indexes
are populated for later invalidation), then returns the value.

Mutations are NOT cached and NOT routed through this interceptor - the
post-commit Invalidator (invalidator.py) handles the write path.
"""

import json
import logging
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from framework.cache.backend import CacheBackend
from framework.cache.key import make_key
from framework.cache.tag import render_template
from framework.dsl.registry import ResolvedQuery
from framework.dsl.schema import CacheProfile

logger = logging.getLogger(__name__)


# Type alias for the inner Phase-1 resolver: (obj, info, **args) -> Any.
QueryResolver = Callable[..., Awaitable[Any]]


class CacheInterceptor:
    """Wraps a query resolver with a CacheBackend lookup."""

    def __init__(self, backend: CacheBackend) -> None:
        self._backend = backend

    def wrap_query(
        self,
        query: ResolvedQuery,
        profile_name: str,
        profile: CacheProfile,
        rule_name: str,
        tag_templates: Sequence[str],
        inner: QueryResolver,
    ) -> QueryResolver:
        """Return a new resolver that performs cache-aside lookup around `inner`.

        - profile_name: key for the per-profile TTLCache in MemoryBackend (Q1).
          Ignored by RedisBackend but kept for API symmetry.
        - profile: the CacheProfile (we read ttl_seconds for the backend.set call).
        - rule_name: the CacheRule name covering this query - used for the rule
          index in set_with_indexes; del_by_rule clears all keys belonging to
          this rule.
        - tag_templates: list of ${args.X} templates from cacheRules.<rule>.tags;
          rendered against args before set_with_indexes; del_by_tag clears all
          keys carrying any rendered tag.
        - inner: the Phase-1 postgres resolver.

        Keys are derived from RAW args via make_key (Q2 - pre-coercion).
        """
        backend = self._backend
        ttl = profile.ttl_seconds

        async def wrapped(_obj: Any, _info: Any, **args: Any) -> Any:
            # Q2: cache key from RAW GraphQL args, before any str->int coercion
            # done by the Phase-1 resolver. Do NOT add coercion here.
            key = make_key(query.name, args)
            cached = await backend.get(key)
            if cached is not None:
                return json.loads(cached)
            result = await inner(_obj, _info, **args)
            payload = json.dumps(
                result, default=str, separators=(",", ":")
            ).encode("utf-8")
            rendered_tags = [render_template(tpl, args) for tpl in tag_templates]
            await backend.set_with_indexes(
                profile_name=profile_name,
                key=key,
                value=payload,
                ttl=ttl,
                rule=rule_name,
                tags=rendered_tags,
            )
            return result

        return wrapped
