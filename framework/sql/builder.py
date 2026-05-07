"""Pure SQL emission: turns ResolvedQuery / ResolvedMutation + arg dicts into
parameterized SQL strings and bound-parameter lists.

No I/O, no asyncpg, no transaction handling. The resolver layer wraps mutations
in transactions and binds parameters; this module only produces text + params.

Conventions (dsl-spec.md section 6, lines 186-229):
- Postgres ``$N`` parameter binding only; values are never substituted textually.
- Column projection ordered alphabetically by GraphQL field name (deterministic).
- ``where_sql`` from the loader already has identifiers rewritten to postgres
  columns and ``${args.X}`` placeholders preserved verbatim - we only renumber.
- ``set_columns`` from the loader is already alphabetical by GraphQL field name
  and keyed by postgres column.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from framework.dsl.registry import ResolvedMutation, ResolvedQuery

# Matches ``${args.NAME}`` placeholders inside loader-emitted where_sql / set values.
_ARG_PLACEHOLDER_RE = re.compile(r"\$\{args\.([A-Za-z_][A-Za-z0-9_]*)\}")


def build_select(
    query: ResolvedQuery, args: Mapping[str, Any]
) -> tuple[str, list[Any]]:
    """Build SELECT SQL + bound parameters for a resolved query.

    Projection is alphabetical by GraphQL field name; if ``where_sql`` is None
    no WHERE clause is emitted.
    """
    projection = _projection(query.columns)
    sql = f"SELECT {projection} FROM {query.table}"
    if query.where_sql is None:
        return sql, []
    where_clause, params = _renumber_where(query.where_sql, query.where_args, args, 1)
    return f"{sql} WHERE {where_clause}", params


def build_mutation(
    mut: ResolvedMutation, args: Mapping[str, Any]
) -> tuple[str, list[Any], str | None, list[Any] | None]:
    """Build mutation SQL.

    Returns ``(write_sql, write_params, read_sql, read_params)``.

    - kind=update: write_sql is UPDATE; read_sql is the follow-up SELECT
      (two-RTT per ADR-034 deferred / roadmap section 2 line 37).
    - kind=insert / kind=delete: read_sql and read_params are None.
    """
    if mut.kind == "insert":
        return (*_build_insert(mut, args), None, None)
    if mut.kind == "delete":
        return (*_build_delete(mut, args), None, None)
    if mut.kind == "update":
        write_sql, write_params = _build_update(mut, args)
        read_sql, read_params = _build_post_update_select(mut, args)
        return write_sql, write_params, read_sql, read_params
    # ResolvedMutation.kind is constrained by the DSL schema; guard regardless.
    raise ValueError(f"unsupported mutation kind: {mut.kind!r}")


# ---------------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------------


def _projection(columns: Mapping[str, str]) -> str:
    """Comma-joined postgres column list, alphabetical by GraphQL field name."""
    return ", ".join(columns[graphql_field] for graphql_field in sorted(columns))


def _renumber_where(
    where_sql: str,
    where_args: tuple[str, ...],
    args: Mapping[str, Any],
    start_index: int,
) -> tuple[str, list[Any]]:
    """Rewrite ``${args.X}`` placeholders to ``$N`` and collect bound params.

    ``where_args`` is the loader-emitted ordered tuple of arg names referenced
    in ``where_sql`` (one entry per ``${args.X}`` occurrence, in textual order).
    """
    counter = {"i": start_index}
    params: list[Any] = []

    def _sub(match: re.Match[str]) -> str:
        name = match.group(1)
        params.append(args[name])
        placeholder = f"${counter['i']}"
        counter["i"] += 1
        return placeholder

    rewritten = _ARG_PLACEHOLDER_RE.sub(_sub, where_sql)
    # Defensive: count must match where_args length the loader produced.
    assert len(params) == len(where_args), (
        f"placeholder count mismatch: rewrote {len(params)} but "
        f"loader recorded {len(where_args)} where_args"
    )
    return rewritten, params


def _extract_arg_name(value_template: str, context: str) -> str:
    """Pull the arg name out of a ``${args.NAME}`` value template."""
    match = _ARG_PLACEHOLDER_RE.fullmatch(value_template)
    if match is None:
        raise ValueError(
            f"{context}: value template {value_template!r} is not a single "
            "'${args.NAME}' placeholder"
        )
    return match.group(1)


def _build_insert(
    mut: ResolvedMutation, args: Mapping[str, Any]
) -> tuple[str, list[Any]]:
    if mut.set_columns is None:
        raise ValueError(f"insert mutation {mut.name!r} has no set_columns")
    columns: list[str] = []
    placeholders: list[str] = []
    params: list[Any] = []
    for index, (pg_col, value_template) in enumerate(mut.set_columns.items(), start=1):
        arg_name = _extract_arg_name(
            value_template, f"mutations.{mut.name}.set.{pg_col}"
        )
        columns.append(pg_col)
        placeholders.append(f"${index}")
        params.append(args[arg_name])
    sql = (
        f"INSERT INTO {mut.table} ({', '.join(columns)}) "
        f"VALUES ({', '.join(placeholders)})"
    )
    return sql, params


def _build_update(
    mut: ResolvedMutation, args: Mapping[str, Any]
) -> tuple[str, list[Any]]:
    if mut.set_columns is None:
        raise ValueError(f"update mutation {mut.name!r} has no set_columns")
    set_fragments: list[str] = []
    params: list[Any] = []
    for index, (pg_col, value_template) in enumerate(mut.set_columns.items(), start=1):
        arg_name = _extract_arg_name(
            value_template, f"mutations.{mut.name}.set.{pg_col}"
        )
        set_fragments.append(f"{pg_col} = ${index}")
        params.append(args[arg_name])
    sql = f"UPDATE {mut.table} SET {', '.join(set_fragments)}"
    if mut.where_sql is not None:
        where_clause, where_params = _renumber_where(
            mut.where_sql, mut.where_args, args, len(params) + 1
        )
        sql = f"{sql} WHERE {where_clause}"
        params.extend(where_params)
    return sql, params


def _build_delete(
    mut: ResolvedMutation, args: Mapping[str, Any]
) -> tuple[str, list[Any]]:
    sql = f"DELETE FROM {mut.table}"
    if mut.where_sql is None:
        return sql, []
    where_clause, params = _renumber_where(mut.where_sql, mut.where_args, args, 1)
    return f"{sql} WHERE {where_clause}", params


def _build_post_update_select(
    mut: ResolvedMutation, args: Mapping[str, Any]
) -> tuple[str, list[Any]]:
    """Follow-up SELECT after an UPDATE - separate statement, fresh ``$1`` numbering."""
    projection = _projection(mut.columns)
    sql = f"SELECT {projection} FROM {mut.table}"
    if mut.where_sql is None:
        return sql, []
    where_clause, params = _renumber_where(mut.where_sql, mut.where_args, args, 1)
    return f"{sql} WHERE {where_clause}", params
