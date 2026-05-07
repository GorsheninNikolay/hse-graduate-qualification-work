"""Ariadne schema-first GraphQL server bound to the DSL OperationRegistry.

Builds resolvers that close over an asyncpg pool and the SQL emitter, and
surfaces multiplicity violations as GraphQL errors[] entries.
"""

import logging
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

import asyncpg  # type: ignore[import-untyped]
from ariadne import MutationType, QueryType, make_executable_schema
from ariadne.asgi import GraphQL

from framework.cache.backend import CacheBackend
from framework.cache.interceptor import CacheInterceptor
from framework.cache.invalidator import Invalidator
from framework.dsl.registry import OperationRegistry, ResolvedMutation, ResolvedQuery
from framework.dsl.schema import CacheProfile
from framework.graphql.errors import MultiplicityViolationError
from framework.sql.builder import build_mutation, build_select

logger = logging.getLogger(__name__)


def build_graphql_app(
    sdl: str,
    registry: OperationRegistry,
    pool: asyncpg.Pool,
    *,
    backends_by_profile: Mapping[str, CacheBackend] | None = None,
) -> GraphQL:
    """Construct the ariadne GraphQL ASGI app from SDL + registry + pool.

    If backends_by_profile is supplied (Phase 2), query resolvers for
    operations covered by a CacheRule are wrapped with CacheInterceptor;
    mutations are chained with Invalidator.after_mutation post-commit.
    Without it, behavior is identical to Phase 1 (no cache).
    """
    query_type = QueryType()
    mutation_type = MutationType()

    # Build a per-op lookup: op_name -> (CacheBackend, profile_name, profile, rule_name, tag_templates).
    op_to_cache_ctx: dict[
        str, tuple[CacheBackend, str, CacheProfile, str, list[str]]
    ] = {}
    if backends_by_profile is not None:
        for rule_name, rule in registry.cache_rules.items():
            profile_name = rule.profile
            profile = registry.cache_profiles[profile_name]
            backend = backends_by_profile[profile_name]
            tag_templates = list(rule.tags or [])
            for op_name in rule.operations:
                op_to_cache_ctx[op_name] = (
                    backend,
                    profile_name,
                    profile,
                    rule_name,
                    tag_templates,
                )

    for name, q in registry.queries.items():
        inner = _make_query_resolver(q, pool)
        if name in op_to_cache_ctx:
            backend, profile_name, profile, rule_name, tag_templates = op_to_cache_ctx[
                name
            ]
            interceptor = CacheInterceptor(backend)
            wrapped_q = interceptor.wrap_query(
                query=q,
                profile_name=profile_name,
                profile=profile,
                rule_name=rule_name,
                tag_templates=tag_templates,
                inner=inner,
            )
            query_type.set_field(name, wrapped_q)
        else:
            query_type.set_field(name, inner)

    # Mutation resolvers: chain Invalidator if backend is supplied AND the
    # mutation has invalidates (any strategy).
    for name, m in registry.mutations.items():
        inner_m = _make_mutation_resolver(m, pool)
        if backends_by_profile is not None and m.invalidates is not None:
            backend = _pick_invalidator_backend(m, registry, backends_by_profile)
            invalidator = Invalidator(backend)
            wrapped_m = _wrap_mutation_with_invalidator(inner_m, invalidator, m)
            mutation_type.set_field(name, wrapped_m)
        else:
            mutation_type.set_field(name, inner_m)

    schema = make_executable_schema(sdl, query_type, mutation_type)
    return GraphQL(schema, debug=False)


def _pick_invalidator_backend(
    m: ResolvedMutation,
    registry: OperationRegistry,
    backends_by_profile: Mapping[str, CacheBackend],
) -> CacheBackend:
    """Choose a CacheBackend for a mutation's Invalidator.

    For 'operation' strategy: return the backend of the first referenced rule's
    profile (deterministic).
    For 'ttl' or 'tag' or no rules: return any backend (ttl no-ops; the 6 Phase-2
    yamls all use a single backend per cell so this is unambiguous). Tag
    invalidations theoretically span profiles, but Phase 2's example cells are
    all single-backend; revisit in Phase 5 / post-MVP.
    """
    if m.invalidates is not None and m.invalidates.strategy == "operation":
        rules = m.invalidates.rules or []
        if rules:
            first_rule = registry.cache_rules[rules[0]]
            return backends_by_profile[first_rule.profile]
    # Fallback: pick any backend (deterministic = first profile name alphabetically).
    first_profile = sorted(backends_by_profile)[0]
    return backends_by_profile[first_profile]


def _wrap_mutation_with_invalidator(
    inner: Callable[..., Awaitable[Any]],
    invalidator: Invalidator,
    m: ResolvedMutation,
) -> Callable[..., Awaitable[Any]]:
    """Chain Invalidator.after_mutation onto a Phase-1 mutation resolver.

    The Phase-1 mutation resolver runs the postgres write inside
    `async with conn.transaction():`; if it returns normally the commit
    succeeded, so we can safely invalidate. Single delete after commit per
    ADR-021 MVP.
    """

    async def wrapped(_obj: Any, _info: Any, **args: Any) -> Any:
        result = await inner(_obj, _info, **args)
        await invalidator.after_mutation(m, args)
        return result

    return wrapped


def _row_to_dict(
    row: asyncpg.Record, columns: Mapping[str, str]
) -> dict[str, Any]:
    """Reverse-map asyncpg Record (postgres column names) to dict (graphql field names)."""
    return {gql_field: row[pg_col] for gql_field, pg_col in columns.items()}


def _coerce_args(args: Mapping[str, Any]) -> dict[str, Any]:
    """GraphQL ID values arrive as Python str; postgres BIGINT columns reject string binds.
    Coerce numeric-looking strings to int so asyncpg can bind them. Phase 1 schema is
    BIGINT-keyed; Phase 2+ may need DSL column-type metadata for richer type bridging.
    """
    out: dict[str, Any] = {}
    for k, v in args.items():
        if isinstance(v, str) and v.lstrip("-").isdigit():
            out[k] = int(v)
        else:
            out[k] = v
    return out


def _make_query_resolver(
    q: ResolvedQuery, pool: asyncpg.Pool
) -> Callable[..., Awaitable[Any]]:
    async def resolver(_obj: Any, _info: Any, **args: Any) -> Any:
        sql, params = build_select(q, _coerce_args(args))
        async with pool.acquire() as conn:
            rows = await conn.fetch(sql, *params)
        if q.multiplicity == "one":
            if not rows:
                return None
            if len(rows) > 1:
                logger.warning(
                    "multiplicity.violation",
                    extra={
                        "operation": q.name,
                        "row_count": len(rows),
                        "args": args,
                    },
                )
                raise MultiplicityViolationError(q.name, len(rows), args)
            return _row_to_dict(rows[0], q.columns)
        return [_row_to_dict(row, q.columns) for row in rows]

    return resolver


def _make_mutation_resolver(
    m: ResolvedMutation, pool: asyncpg.Pool
) -> Callable[..., Awaitable[Any]]:
    async def resolver(_obj: Any, _info: Any, **args: Any) -> Any:
        write_sql, write_params, read_sql, read_params = build_mutation(m, _coerce_args(args))
        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(write_sql, *write_params)
                if read_sql is not None:
                    rows = await conn.fetch(read_sql, *(read_params or []))
                    if not rows:
                        return None
                    return _row_to_dict(rows[0], m.columns)
        return None

    return resolver
