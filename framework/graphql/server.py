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

from framework.dsl.registry import OperationRegistry, ResolvedMutation, ResolvedQuery
from framework.graphql.errors import MultiplicityViolationError
from framework.sql.builder import build_mutation, build_select

logger = logging.getLogger(__name__)


def build_graphql_app(
    sdl: str,
    registry: OperationRegistry,
    pool: asyncpg.Pool,
) -> GraphQL:
    """Construct the ariadne GraphQL ASGI app from SDL + registry + pool."""
    query_type = QueryType()
    mutation_type = MutationType()

    for name, q in registry.queries.items():
        query_type.set_field(name, _make_query_resolver(q, pool))
    for name, m in registry.mutations.items():
        mutation_type.set_field(name, _make_mutation_resolver(m, pool))

    schema = make_executable_schema(sdl, query_type, mutation_type)
    return GraphQL(schema, debug=False)


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
