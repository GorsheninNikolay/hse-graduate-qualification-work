"""Immutable post-validation registry: column names already rewritten,
references already resolved. reads this same shape."""

from collections.abc import Mapping
from dataclasses import dataclass

from framework.dsl.schema import (
    CacheProfile,
    CacheRule,
    InvalidatesSpec,
    TableMapping,
)


@dataclass(frozen=True, slots=True)
class ResolvedQuery:
    name: str
    type_name: str
    table: str
    columns: Mapping[str, str]
    primary_key: str
    multiplicity: str
    where_sql: str | None
    where_args: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ResolvedMutation:
    name: str
    type_name: str
    table: str
    columns: Mapping[str, str]
    primary_key: str
    kind: str
    set_columns: Mapping[str, str] | None
    where_sql: str | None
    where_args: tuple[str, ...]
    invalidates: InvalidatesSpec | None


@dataclass(frozen=True, slots=True)
class OperationRegistry:
    queries: Mapping[str, ResolvedQuery]
    mutations: Mapping[str, ResolvedMutation]
    tables: Mapping[str, TableMapping]
    cache_profiles: Mapping[str, CacheProfile]
    cache_rules: Mapping[str, CacheRule]
