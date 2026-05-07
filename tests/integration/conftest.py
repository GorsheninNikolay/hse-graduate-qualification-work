"""Shared helpers for the tests/integration/ suite.

Centralizes:
  - postgres + redis reachability checks (integration tests skip if either
    port is unreachable),
  - the env-var defaults the framework.cli lifespan reads at boot time,
  - the `_booted_app` async-context-manager that drives Starlette's lifespan
    around an httpx ASGITransport client for a chosen DSL yaml.

Both T10 (test_cache_combinations.py) and T11 (test_cache_counters_idempotence.py)
import these helpers to avoid duplicating boot scaffolding.

The lifespan reads DSL_PATH at lifespan-enter time (not import time), so
`_booted_app(yaml_path)` swaps the env var per call to drive different
cache topologies through the SAME framework.cli.app object.
"""

from __future__ import annotations

import os
import pathlib
import socket
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]


def _postgres_reachable() -> bool:
    try:
        with socket.create_connection(("127.0.0.1", 5432), timeout=1):
            return True
    except OSError:
        return False


def _redis_reachable() -> bool:
    try:
        with socket.create_connection(("127.0.0.1", 6379), timeout=1):
            return True
    except OSError:
        return False


# Module-level skip guard for any test file that imports from this conftest's
# helpers and uses _booted_app — the lifespan opens an asyncpg pool, so
# postgres must be reachable. Redis-specific guards live on per-test cases
# that use redis-backed yamls.
postgres_skip = pytest.mark.skipif(
    not _postgres_reachable(),
    reason="postgres not reachable on 127.0.0.1:5432; run `make up` first",
)

redis_skip = pytest.mark.skipif(
    not _redis_reachable(),
    reason="redis not reachable on 127.0.0.1:6379; run `make up` first",
)


# Env-var defaults must be set BEFORE importing framework.cli because the
# Starlette `app` is constructed at import time. The lifespan re-reads
# DSL_PATH on each enter, so swapping it later still works.
os.environ.setdefault(
    "POSTGRES_DSN",
    "postgresql://postgres:postgres@localhost:5432/postgres",
)
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault("SCHEMA_PATH", str(REPO_ROOT / "examples" / "schema.graphql"))
os.environ.setdefault(
    "DSL_PATH", str(REPO_ROOT / "examples" / "graphql-api-no-cache.yaml")
)

from framework.cli import app  # noqa: E402  (after env-var defaults)


@asynccontextmanager
async def _booted_app(yaml_path: pathlib.Path) -> AsyncIterator[httpx.AsyncClient]:
    """Boot framework.cli.app against `yaml_path` and yield an httpx client.

    Sets DSL_PATH for this scope, drives the Starlette lifespan, and yields
    an ASGITransport-backed AsyncClient. After the context exits, the
    lifespan's finally block closes the asyncpg pool and the redis client
    (if any), and clears _state.
    """
    previous = os.environ.get("DSL_PATH")
    os.environ["DSL_PATH"] = str(yaml_path)
    try:
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://test"
            ) as client:
                yield client
    finally:
        if previous is None:
            os.environ.pop("DSL_PATH", None)
        else:
            os.environ["DSL_PATH"] = previous


async def _gql(client: httpx.AsyncClient, query: str) -> dict[str, Any]:
    """POST a GraphQL query and return the data dict; assert no errors."""
    # Trailing slash so Starlette's Mount doesn't emit a 307 redirect.
    response = await client.post("/graphql/", json={"query": query})
    assert response.status_code == 200, response.text
    body = response.json()
    assert "errors" not in body, body
    data = body["data"]
    assert isinstance(data, dict)
    return data
