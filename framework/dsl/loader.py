"""Public DSL loader: parses YAML + SDL, validates, resolves, returns OperationRegistry.

Public surface is exactly one function: ``load(yaml_path, schema_path)``.
All other module-level names are private helpers prefixed with ``_``.
"""

from __future__ import annotations

import pathlib
import re
from collections.abc import Mapping
from types import MappingProxyType

# PyYAML ships no inline types and types-PyYAML is not in dev deps yet (T7 may add it).
import yaml  # type: ignore[import-untyped]
from graphql import (
    GraphQLError,
    GraphQLList,
    GraphQLNonNull,
    GraphQLObjectType,
    GraphQLSchema,
    build_schema,
)
from pydantic import ValidationError

from framework.dsl.errors import DSLError, DSLErrorCode
from framework.dsl.registry import OperationRegistry, ResolvedMutation, ResolvedQuery
from framework.dsl.schema import (
    Document,
    MutationSpec,
    TableMapping,
)

# DSL spec section 6 lines 222-229: predicate operator allowlist.
_KEYWORDS: frozenset[str] = frozenset({"AND", "OR", "NOT", "IN", "IS", "NULL"})
_OPERATORS: frozenset[str] = frozenset({"=", "!=", "<", "<=", ">", ">="})
_PUNCT: frozenset[str] = frozenset({"(", ")", ","})

# Tokenize: placeholders, whitespace runs, multi-char operators, single-char operators / punct.
_TOKEN_RE = re.compile(r"(\$\{[^}]+\}|\s+|<=|>=|!=|=|<|>|\(|\)|,)")
_ARG_RE = re.compile(r"^\$\{args\.([A-Za-z_][A-Za-z0-9_]*)\}$")
_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_NUMERIC_RE = re.compile(r"^-?\d+(\.\d+)?$")
_STRING_RE = re.compile(r"^'[^']*'$")


def load(yaml_path: pathlib.Path, schema_path: pathlib.Path) -> OperationRegistry:
    """Load + validate + resolve a DSL document. Fail-fast on first error."""
    parsed = _read_yaml(yaml_path)
    document = _validate_document(parsed)
    sdl_schema = _read_sdl(schema_path)

    _check_operation_refs(document)
    _check_invalidate_strategy(document)
    _check_profile_refs(document)
    _check_profile_max_entries(document)
    _check_nested_mapped_types(document, sdl_schema)

    queries = _resolve_queries(document)
    mutations = _resolve_mutations(document)

    return OperationRegistry(
        queries=MappingProxyType(queries),
        mutations=MappingProxyType(mutations),
        tables=MappingProxyType(dict(document.dataSources.postgres.tables)),
        cache_profiles=MappingProxyType(dict(document.cacheProfiles)),
        cache_rules=MappingProxyType(dict(document.cacheRules)),
    )


# ---------------------------------------------------------------------------
# Step 1: YAML read + parse.
# ---------------------------------------------------------------------------


def _read_yaml(yaml_path: pathlib.Path) -> object:
    try:
        text = yaml_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise DSLError(DSLErrorCode.YAML_PARSE, str(yaml_path), str(exc)) from exc
    try:
        return yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise DSLError(DSLErrorCode.YAML_PARSE, str(yaml_path), str(exc)) from exc


# ---------------------------------------------------------------------------
# Step 2: pydantic validate.
# ---------------------------------------------------------------------------


def _validate_document(parsed: object) -> Document:
    try:
        return Document.model_validate(parsed)
    except ValidationError as exc:
        first = exc.errors()[0]
        loc_parts = [str(p) for p in first.get("loc", ())]
        path = ".".join(loc_parts)
        message = str(first.get("msg", "validation error"))
        raise DSLError(DSLErrorCode.YAML_SCHEMA, path, message) from exc


# ---------------------------------------------------------------------------
# Step 3: SDL read + parse. Errors collapse onto YAML_SCHEMA.
# ---------------------------------------------------------------------------


def _read_sdl(schema_path: pathlib.Path) -> GraphQLSchema:
    try:
        text = schema_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise DSLError(DSLErrorCode.YAML_SCHEMA, str(schema_path), str(exc)) from exc
    try:
        return build_schema(text)
    except GraphQLError as exc:
        raise DSLError(DSLErrorCode.YAML_SCHEMA, str(schema_path), str(exc)) from exc


# ---------------------------------------------------------------------------
# Step 4a: cacheRules.<r>.operations[i] must reference a known query.
# Per roadmap line 107, dup.operation also collapses onto REF_OPERATION.
# ---------------------------------------------------------------------------


def _check_operation_refs(document: Document) -> None:
    seen_operations: dict[str, str] = {}
    for rule_name, rule in document.cacheRules.items():
        for idx, op_name in enumerate(rule.operations):
            path = f"cacheRules.{rule_name}.operations[{idx}]"
            if op_name not in document.queries:
                raise DSLError(
                    DSLErrorCode.REF_OPERATION,
                    path,
                    f"unknown query '{op_name}'",
                )
            if op_name in seen_operations:
                raise DSLError(
                    DSLErrorCode.REF_OPERATION,
                    path,
                    f"query '{op_name}' is already covered by cacheRule "
                    f"'{seen_operations[op_name]}'",
                )
            seen_operations[op_name] = rule_name


# ---------------------------------------------------------------------------
# Step 4b: invalidates.strategy <-> rules/tags consistency, and ref.rule.
# ---------------------------------------------------------------------------


def _check_invalidate_strategy(document: Document) -> None:
    for op_name, mutation in document.mutations.items():
        spec = mutation.invalidates
        if spec is None:
            continue
        path = f"mutations.{op_name}.invalidates"
        if spec.strategy == "tag":
            if not spec.tags:
                raise DSLError(
                    DSLErrorCode.YAML_SCHEMA,
                    path,
                    "strategy=tag requires 'tags'",
                )
            if spec.rules is not None:
                raise DSLError(
                    DSLErrorCode.YAML_SCHEMA,
                    path,
                    "strategy=tag forbids 'rules'",
                )
        elif spec.strategy == "operation":
            if not spec.rules:
                raise DSLError(
                    DSLErrorCode.YAML_SCHEMA,
                    path,
                    "strategy=operation requires 'rules'",
                )
            if spec.tags is not None:
                raise DSLError(
                    DSLErrorCode.YAML_SCHEMA,
                    path,
                    "strategy=operation forbids 'tags'",
                )
            for idx, rule_name in enumerate(spec.rules):
                rule_path = f"mutations.{op_name}.invalidates.rules[{idx}]"
                if rule_name not in document.cacheRules:
                    raise DSLError(
                        DSLErrorCode.REF_RULE,
                        rule_path,
                        f"unknown rule '{rule_name}'",
                    )
        else:  # ttl
            if spec.rules is not None:
                raise DSLError(
                    DSLErrorCode.YAML_SCHEMA,
                    path,
                    "strategy=ttl forbids 'rules'",
                )
            if spec.tags is not None:
                raise DSLError(
                    DSLErrorCode.YAML_SCHEMA,
                    path,
                    "strategy=ttl forbids 'tags'",
                )


# ---------------------------------------------------------------------------
# Step 5: cacheRules.<r>.profile must reference a known cache profile.
# ---------------------------------------------------------------------------


def _check_profile_refs(document: Document) -> None:
    for rule_name, rule in document.cacheRules.items():
        if rule.profile not in document.cacheProfiles:
            raise DSLError(
                DSLErrorCode.REF_PROFILE,
                f"cacheRules.{rule_name}.profile",
                f"unknown profile '{rule.profile}'",
            )


# ---------------------------------------------------------------------------
# Step 6: cacheProfiles.<p>.max_entries required when backend is in_memory.
# ---------------------------------------------------------------------------


def _check_profile_max_entries(document: Document) -> None:
    for name, profile in document.cacheProfiles.items():
        if profile.backend == "in_memory" and profile.max_entries is None:
            raise DSLError(
                DSLErrorCode.YAML_SCHEMA,
                f"cacheProfiles.{name}",
                "max_entries is required for in_memory backend",
            )


# ---------------------------------------------------------------------------
# Step 7: ADR-036 nested-mapped-type check (dsl-spec lines 127-129).
# A field of a mapped type may not itself reference another mapped type.
# ---------------------------------------------------------------------------


def _check_nested_mapped_types(document: Document, sdl_schema: GraphQLSchema) -> None:
    mapped_type_names = set(document.dataSources.postgres.tables.keys())
    for type_name in mapped_type_names:
        gql_type = sdl_schema.get_type(type_name)
        if not isinstance(gql_type, GraphQLObjectType):
            raise DSLError(
                DSLErrorCode.YAML_SCHEMA,
                f"dataSources.postgres.tables.{type_name}",
                f"type '{type_name}' is not an object type in the SDL",
            )
        for field_name, field in gql_type.fields.items():
            inner = _unwrap_type(field.type)
            inner_name = getattr(inner, "name", None)
            if inner_name in mapped_type_names:
                raise DSLError(
                    DSLErrorCode.YAML_SCHEMA,
                    f"dataSources.postgres.tables.{type_name}",
                    (
                        f"field '{field_name}' has nested mapped type "
                        f"'{inner_name}'; ADR-036 requires scalar fields only"
                    ),
                )


def _unwrap_type(gql_type: object) -> object:
    while isinstance(gql_type, (GraphQLNonNull, GraphQLList)):
        gql_type = gql_type.of_type
    return gql_type


# ---------------------------------------------------------------------------
# Step 8: where/set rewriting.
# ---------------------------------------------------------------------------


def _resolve_queries(document: Document) -> dict[str, ResolvedQuery]:
    tables = document.dataSources.postgres.tables
    resolved: dict[str, ResolvedQuery] = {}
    for op_name, spec in document.queries.items():
        table = _require_mapped_type(tables, spec.type, f"queries.{op_name}.type")
        where_sql, where_args = _rewrite_where(
            spec.where, table.columns, f"queries.{op_name}.where"
        )
        resolved[op_name] = ResolvedQuery(
            name=op_name,
            type_name=spec.type,
            table=table.table,
            columns=MappingProxyType(dict(table.columns)),
            primary_key=table.primaryKey,
            multiplicity=spec.multiplicity,
            where_sql=where_sql,
            where_args=where_args,
        )
    return resolved


def _resolve_mutations(document: Document) -> dict[str, ResolvedMutation]:
    tables = document.dataSources.postgres.tables
    resolved: dict[str, ResolvedMutation] = {}
    for op_name, spec in document.mutations.items():
        table = _require_mapped_type(tables, spec.type, f"mutations.{op_name}.type")
        _require_set_for_kind(spec, op_name)
        set_columns = _rewrite_set(spec.set, table.columns, f"mutations.{op_name}.set")
        where_sql, where_args = _rewrite_where(
            spec.where, table.columns, f"mutations.{op_name}.where"
        )
        resolved[op_name] = ResolvedMutation(
            name=op_name,
            type_name=spec.type,
            table=table.table,
            columns=MappingProxyType(dict(table.columns)),
            primary_key=table.primaryKey,
            kind=spec.kind,
            set_columns=set_columns,
            where_sql=where_sql,
            where_args=where_args,
            invalidates=spec.invalidates,
        )
    return resolved


def _require_mapped_type(
    tables: Mapping[str, TableMapping], type_name: str, path: str
) -> TableMapping:
    if type_name not in tables:
        raise DSLError(
            DSLErrorCode.YAML_SCHEMA,
            path,
            f"type '{type_name}' has no postgres table mapping",
        )
    return tables[type_name]


def _require_set_for_kind(spec: MutationSpec, op_name: str) -> None:
    if spec.kind in ("insert", "update") and spec.set is None:
        raise DSLError(
            DSLErrorCode.YAML_SCHEMA,
            f"mutations.{op_name}.set",
            f"kind={spec.kind} requires 'set'",
        )
    if spec.kind == "delete" and spec.set is not None:
        raise DSLError(
            DSLErrorCode.YAML_SCHEMA,
            f"mutations.{op_name}.set",
            "kind=delete forbids 'set'",
        )


def _rewrite_set(
    spec_set: Mapping[str, str] | None,
    columns: Mapping[str, str],
    path: str,
) -> Mapping[str, str] | None:
    if spec_set is None:
        return None
    rewritten: dict[str, str] = {}
    for graphql_field in sorted(spec_set):
        value = spec_set[graphql_field]
        if graphql_field not in columns:
            raise DSLError(
                DSLErrorCode.YAML_SCHEMA,
                f"{path}.{graphql_field}",
                f"unknown field '{graphql_field}' (not in columns map)",
            )
        rewritten[columns[graphql_field]] = value
    return MappingProxyType(rewritten)


def _rewrite_where(
    where: str | None,
    columns: Mapping[str, str],
    path: str,
) -> tuple[str | None, tuple[str, ...]]:
    if where is None:
        return None, ()
    tokens = [t for t in _TOKEN_RE.split(where) if t != ""]
    output: list[str] = []
    args: list[str] = []
    prev_meaningful: str | None = None
    for token in tokens:
        if token.isspace():
            output.append(token)
            continue
        kind = _classify(token)
        if kind == "arg":
            match = _ARG_RE.match(token)
            if match is None:
                raise DSLError(
                    DSLErrorCode.YAML_SCHEMA,
                    path,
                    (
                        f"placeholder '{token}' is not a valid '${{args.NAME}}' "
                        "reference"
                    ),
                )
            args.append(match.group(1))
            output.append(token)
        elif kind == "operator" or kind == "punct":
            if token == "(" and prev_meaningful is not None and _looks_like_ident(
                prev_meaningful
            ):
                raise DSLError(
                    DSLErrorCode.YAML_SCHEMA,
                    path,
                    (
                        f"function call '{prev_meaningful}(...)' not allowed in "
                        "where; dsl-spec section 6 allowlist only"
                    ),
                )
            output.append(token)
        elif kind == "keyword":
            output.append(token)
        elif kind == "numeric" or kind == "string":
            output.append(token)
        elif kind == "identifier":
            if token not in columns:
                raise DSLError(
                    DSLErrorCode.YAML_SCHEMA,
                    path,
                    (
                        f"unknown identifier '{token}' in where; not in columns "
                        "map and not a permitted keyword"
                    ),
                )
            output.append(columns[token])
        else:
            raise DSLError(
                DSLErrorCode.YAML_SCHEMA,
                path,
                (
                    f"unsupported operator/syntax '{token}' in where; "
                    "dsl-spec section 6 allowlist only"
                ),
            )
        prev_meaningful = token
    return "".join(output), tuple(args)


def _classify(token: str) -> str:
    if _ARG_RE.match(token):
        return "arg"
    if token in _OPERATORS:
        return "operator"
    if token in _PUNCT:
        return "punct"
    if token.upper() in _KEYWORDS and token.isalpha():
        return "keyword"
    if _NUMERIC_RE.match(token):
        return "numeric"
    if _STRING_RE.match(token):
        return "string"
    if _IDENT_RE.match(token):
        return "identifier"
    return "unsupported"


def _looks_like_ident(token: str) -> bool:
    return _IDENT_RE.match(token) is not None and token.upper() not in _KEYWORDS
