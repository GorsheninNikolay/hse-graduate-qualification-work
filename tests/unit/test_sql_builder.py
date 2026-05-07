"""Unit tests for framework.sql.builder.

Drives build_select / build_mutation off the registry produced by load() of the
Phase 1 baseline yaml. No fakes, no I/O.
"""

from __future__ import annotations

import dataclasses
import pathlib
import re

from framework.dsl.loader import load
from framework.sql.builder import build_mutation, build_select

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
BASELINE_YAML = REPO_ROOT / "examples" / "graphql-api-no-cache.yaml"
SCHEMA_PATH = REPO_ROOT / "examples" / "schema.graphql"


def _normalize(sql: str) -> str:
    return re.sub(r"\s+", " ", sql).strip()


def test_build_select_one() -> None:
    registry = load(BASELINE_YAML, SCHEMA_PATH)
    sql, params = build_select(registry.queries["getUser"], {"id": "1"})
    assert _normalize(sql) == "SELECT id, full_name, team_id FROM users WHERE id = $1"
    assert params == ["1"]


def test_build_select_many_alphabetical_columns() -> None:
    registry = load(BASELINE_YAML, SCHEMA_PATH)
    sql, params = build_select(registry.queries["listUsersByTeam"], {"teamId": "1"})
    select_clause = re.match(r"SELECT\s+(.+?)\s+FROM", sql, re.IGNORECASE)
    assert select_clause is not None
    columns = [c.strip() for c in select_clause.group(1).split(",")]
    # GraphQL fields alphabetical: id, name, teamId -> postgres: id, full_name, team_id.
    assert columns == ["id", "full_name", "team_id"]
    assert params == ["1"]


def test_build_mutation_update_returns_two_statements() -> None:
    registry = load(BASELINE_YAML, SCHEMA_PATH)
    write_sql, write_params, read_sql, read_params = build_mutation(
        registry.mutations["updateUserName"], {"id": "1", "name": "renamed"}
    )
    assert _normalize(write_sql) == "UPDATE users SET full_name = $1 WHERE id = $2"
    assert write_params == ["renamed", "1"]
    assert read_sql is not None
    assert _normalize(read_sql) == (
        "SELECT id, full_name, team_id FROM users WHERE id = $1"
    )
    assert read_params == ["1"]


def test_build_select_no_where_clause() -> None:
    registry = load(BASELINE_YAML, SCHEMA_PATH)
    no_where_query = dataclasses.replace(
        registry.queries["getUser"], where_sql=None, where_args=()
    )
    sql, params = build_select(no_where_query, {})
    assert _normalize(sql) == "SELECT id, full_name, team_id FROM users"
    assert params == []
