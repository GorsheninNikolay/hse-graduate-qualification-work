"""Unit tests for framework.dsl.loader.load.

Each test feeds a tweaked copy of the known-good Phase 1 baseline YAML through
the loader and asserts the public DSLError contract (5 codes total).
"""

from __future__ import annotations

import pathlib

import pytest
import yaml  # type: ignore[import-untyped]

from framework.dsl.errors import DSLError, DSLErrorCode
from framework.dsl.loader import load

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
BASELINE_YAML = REPO_ROOT / "examples" / "graphql-api-no-cache.yaml"
SCHEMA_PATH = REPO_ROOT / "examples" / "schema.graphql"

_BASELINE_TEXT = BASELINE_YAML.read_text(encoding="utf-8")


def _baseline_doc() -> dict[str, object]:
    """Fresh deep-mutable copy of the baseline parsed YAML."""
    parsed = yaml.safe_load(_BASELINE_TEXT)
    assert isinstance(parsed, dict)
    return parsed


def _write(tmp_path: pathlib.Path, doc: dict[str, object]) -> pathlib.Path:
    target = tmp_path / "doc.yaml"
    target.write_text(yaml.safe_dump(doc), encoding="utf-8")
    return target


def test_load_no_cache_yaml_succeeds() -> None:
    registry = load(BASELINE_YAML, SCHEMA_PATH)
    assert set(registry.queries) == {"getUser", "listUsersByTeam", "getTeam"}
    assert set(registry.mutations) == {"updateUserName", "moveUserToTeam"}
    assert dict(registry.cache_profiles) == {}
    assert dict(registry.cache_rules) == {}


def test_load_yaml_parse_error_surfaces_yaml_parse(tmp_path: pathlib.Path) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text("not: [valid yaml", encoding="utf-8")
    with pytest.raises(DSLError) as exc_info:
        load(bad, SCHEMA_PATH)
    assert exc_info.value.code == DSLErrorCode.YAML_PARSE


def test_load_unknown_field_surfaces_yaml_schema(tmp_path: pathlib.Path) -> None:
    doc = _baseline_doc()
    doc["foobar"] = "extra"
    with pytest.raises(DSLError) as exc_info:
        load(_write(tmp_path, doc), SCHEMA_PATH)
    assert exc_info.value.code == DSLErrorCode.YAML_SCHEMA


def test_load_version_2_surfaces_yaml_schema(tmp_path: pathlib.Path) -> None:
    doc = _baseline_doc()
    doc["version"] = 2
    with pytest.raises(DSLError) as exc_info:
        load(_write(tmp_path, doc), SCHEMA_PATH)
    assert exc_info.value.code == DSLErrorCode.YAML_SCHEMA


def test_load_invalid_multiplicity_surfaces_yaml_schema(tmp_path: pathlib.Path) -> None:
    doc = _baseline_doc()
    queries = doc["queries"]
    assert isinstance(queries, dict)
    queries["getUser"]["multiplicity"] = "maybe"
    with pytest.raises(DSLError) as exc_info:
        load(_write(tmp_path, doc), SCHEMA_PATH)
    assert exc_info.value.code == DSLErrorCode.YAML_SCHEMA


def test_load_empty_cache_rule_operations_surfaces_yaml_schema(
    tmp_path: pathlib.Path,
) -> None:
    doc = _baseline_doc()
    doc["cacheProfiles"] = {
        "hot": {"backend": "redis", "eviction": "lru", "ttl_seconds": 60},
    }
    doc["cacheRules"] = {"foo": {"operations": [], "profile": "hot"}}
    with pytest.raises(DSLError) as exc_info:
        load(_write(tmp_path, doc), SCHEMA_PATH)
    assert exc_info.value.code == DSLErrorCode.YAML_SCHEMA


def test_load_unsupported_operator_in_where_surfaces_yaml_schema(
    tmp_path: pathlib.Path,
) -> None:
    doc = _baseline_doc()
    queries = doc["queries"]
    assert isinstance(queries, dict)
    queries["getUser"]["where"] = "name LIKE ${args.q}"
    with pytest.raises(DSLError) as exc_info:
        load(_write(tmp_path, doc), SCHEMA_PATH)
    assert exc_info.value.code == DSLErrorCode.YAML_SCHEMA


def test_load_unknown_field_in_where_surfaces_yaml_schema(
    tmp_path: pathlib.Path,
) -> None:
    doc = _baseline_doc()
    queries = doc["queries"]
    assert isinstance(queries, dict)
    queries["getUser"]["where"] = "nonexistent = ${args.id}"
    with pytest.raises(DSLError) as exc_info:
        load(_write(tmp_path, doc), SCHEMA_PATH)
    assert exc_info.value.code == DSLErrorCode.YAML_SCHEMA


def test_load_unknown_profile_surfaces_ref_profile(tmp_path: pathlib.Path) -> None:
    doc = _baseline_doc()
    doc["cacheProfiles"] = {
        "hot": {"backend": "redis", "eviction": "lru", "ttl_seconds": 60},
    }
    doc["cacheRules"] = {
        "r": {"operations": ["getUser"], "profile": "missing"},
    }
    with pytest.raises(DSLError) as exc_info:
        load(_write(tmp_path, doc), SCHEMA_PATH)
    assert exc_info.value.code == DSLErrorCode.REF_PROFILE


def test_load_unknown_rule_surfaces_ref_rule(tmp_path: pathlib.Path) -> None:
    doc = _baseline_doc()
    doc["cacheProfiles"] = {
        "hot": {"backend": "redis", "eviction": "lru", "ttl_seconds": 60},
    }
    doc["cacheRules"] = {
        "r": {"operations": ["getUser"], "profile": "hot"},
    }
    mutations = doc["mutations"]
    assert isinstance(mutations, dict)
    mutations["updateUserName"]["invalidates"] = {
        "strategy": "operation",
        "rules": ["doesNotExist"],
    }
    with pytest.raises(DSLError) as exc_info:
        load(_write(tmp_path, doc), SCHEMA_PATH)
    assert exc_info.value.code == DSLErrorCode.REF_RULE


def test_load_dup_operation_surfaces_ref_operation(tmp_path: pathlib.Path) -> None:
    doc = _baseline_doc()
    doc["cacheProfiles"] = {
        "hot": {"backend": "redis", "eviction": "lru", "ttl_seconds": 60},
    }
    doc["cacheRules"] = {
        "a": {"operations": ["getUser"], "profile": "hot"},
        "b": {"operations": ["getUser"], "profile": "hot"},
    }
    with pytest.raises(DSLError) as exc_info:
        load(_write(tmp_path, doc), SCHEMA_PATH)
    assert exc_info.value.code == DSLErrorCode.REF_OPERATION
