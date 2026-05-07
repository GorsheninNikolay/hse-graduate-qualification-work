"""Post-commit invalidator dispatch (Phase 2).

Called by the mutation resolver wrapper (in framework/graphql/server.py) after
the postgres COMMIT succeeds. Single-delete-after-commit per ADR-021 MVP:
double-delete is deferred and documented as a known limitation.

Strategy dispatch:
  - ttl: no-op. The backend's TTL evicts entries naturally; mutations don't
    explicitly invalidate.
  - operation: for each rule in mut.invalidates.rules, call backend.del_by_rule.
    Removes all cache entries belonging to that rule.
  - tag: render each tag template against the mutation's args, call
    backend.del_by_tag for each rendered string.
"""

import logging
from collections.abc import Mapping
from typing import Any

from framework.cache.backend import CacheBackend
from framework.cache.tag import render_template
from framework.dsl.registry import ResolvedMutation

logger = logging.getLogger(__name__)


class Invalidator:
    """Dispatches mutation invalidation per the strategy declared in the DSL."""

    def __init__(self, backend: CacheBackend) -> None:
        self._backend = backend

    async def after_mutation(
        self, mut: ResolvedMutation, args: Mapping[str, Any]
    ) -> None:
        spec = mut.invalidates
        if spec is None:
            return
        strategy = spec.strategy
        if strategy == "ttl":
            return  # backend's TTL handles expiry
        if strategy == "operation":
            rules = spec.rules or []
            for rule in rules:
                await self._backend.del_by_rule(rule)
            return
        if strategy == "tag":
            templates = spec.tags or []
            for tpl in templates:
                rendered = render_template(tpl, args)
                await self._backend.del_by_tag(rendered)
            return
        # Pydantic Literal narrows to the 3 strategies above; this branch is
        # unreachable but documented for readers.
        logger.warning("unknown strategy %r; skipping invalidation", strategy)
