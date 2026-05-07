"""Pydantic v2 model tree mirroring the DSL surface in docs/architecture/dsl-spec.md."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field


class _Strict(BaseModel):
    """Base for every DSL node: forbid unknown fields, freeze, no field aliasing."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class SidecarListen(_Strict):
    loopback: str


class SidecarBoot(_Strict):
    postgres_timeout: str


class RedisCacheConfig(_Strict):
    circuit_threshold: int = 10
    circuit_cooldown: str = "5s"


class SidecarCacheConfig(_Strict):
    redis: RedisCacheConfig | None = None


class SidecarConfig(_Strict):
    listen: SidecarListen
    boot: SidecarBoot
    cache: SidecarCacheConfig | None = None


class TableMapping(_Strict):
    table: str
    primaryKey: str
    columns: dict[str, str]


class PostgresSource(_Strict):
    type: Literal["postgres"]
    dsn: str
    tables: dict[str, TableMapping]


class DataSources(_Strict):
    postgres: PostgresSource


class QuerySpec(_Strict):
    type: str
    source: str
    multiplicity: Literal["one", "many"]
    where: str | None = None


class InvalidatesSpec(_Strict):
    strategy: Literal["ttl", "operation", "tag"]
    rules: list[str] | None = None
    tags: list[str] | None = None


class MutationSpec(_Strict):
    type: str
    source: str
    kind: Literal["insert", "update", "delete"]
    set: dict[str, str] | None = None
    where: str | None = None
    invalidates: InvalidatesSpec | None = None


class InvalidationConfig(_Strict):
    double_delete_delay_ms: Annotated[int, Field(ge=0)] = 100


class CacheProfile(_Strict):
    backend: Literal["redis", "in_memory"]
    eviction: Literal["lru", "lfu", "none"]
    ttl_seconds: Annotated[int, Field(ge=0)]
    max_entries: Annotated[int, Field(gt=0)] | None = None
    invalidation: InvalidationConfig | None = None


class CacheRule(_Strict):
    operations: Annotated[list[str], Field(min_length=1)]
    profile: str
    cache_key: str | None = None
    tags: list[str] | None = None


class Document(_Strict):
    version: Literal[1]
    sidecar: SidecarConfig
    dataSources: DataSources
    queries: dict[str, QuerySpec]
    mutations: dict[str, MutationSpec] = Field(default_factory=dict)
    cacheProfiles: dict[str, CacheProfile] = Field(default_factory=dict)
    cacheRules: dict[str, CacheRule] = Field(default_factory=dict)
