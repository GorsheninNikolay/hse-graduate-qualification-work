"""Phase 2 acceptance gate: 6-cell scenario test (3 strategies x 2 backends).

These tests drive the framework's Starlette lifespan in-process via
httpx.ASGITransport. Each cell:
  - sets DSL_PATH to the matching yaml,
  - boots the framework (which instantiates the backends via the conditional
    Q8 logic in framework/cli.py),
  - exercises GET-miss-DB-set; repeat-hit; mutation; next-GET (per strategy).

Requires `make up` running (postgres + redis on localhost ports). Module
SKIPS itself if either is unreachable.
"""

from __future__ import annotations

import asyncio
import os
import pathlib
import socket
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]


def _port_reachable(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            return True
    except OSError:
        return False


pytestmark = pytest.mark.skipif(
    not (_port_reachable(5432) and _port_reachable(6379)),
    reason="postgres or redis not reachable; run `make up` first",
)


# Defaults must be set BEFORE framework.cli is imported because _lifespan reads
# them at call time but the module-level Starlette app is constructed at import.
os.environ.setdefault(
    "POSTGRES_DSN", "postgresql://postgres:postgres@localhost:5432/postgres"
)
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault("SCHEMA_PATH", str(REPO_ROOT / "examples" / "schema.graphql"))


@asynccontextmanager
async def _booted_app(dsl_path: pathlib.Path) -> AsyncIterator[httpx.AsyncClient]:
    """Drive Starlette lifespan with an explicit DSL_PATH around an httpx client.

    Sets DSL_PATH on os.environ before entering the lifespan_context so that
    framework.cli._lifespan picks up the per-test yaml on each enter. The
    module-level `app` is constructed once at import time (no DB needed); the
    lifespan is where backends + pool + counters are instantiated, so per-test
    state is fresh on each enter and torn down cleanly on exit.

    Restores DSL_PATH on exit so we do not leak this test's cache-enabled
    yaml into subsequent test files (Phase 1 integration assumes the no-cache
    yaml that its own module-level setdefault establishes).
    """
    previous = os.environ.get("DSL_PATH")
    os.environ["DSL_PATH"] = str(dsl_path)
    # Lazy import: the module-level Starlette app is fine to share across tests
    # because the lifespan's _state dict is fully populated/cleared per enter.
    from framework.cli import app

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
    # Trailing slash so Starlette's Mount doesn't emit a 307 redirect.
    response = await client.post("/graphql/", json={"query": query})
    assert response.status_code == 200, response.text
    body = response.json()
    assert "errors" not in body, body
    data = body["data"]
    assert isinstance(data, dict)
    return data


CELLS = [
    ("ttl", "redis"),
    ("ttl", "memory"),
    ("operation", "redis"),
    ("operation", "memory"),
    ("tag", "redis"),
    ("tag", "memory"),
]


def _yaml_for_cell(strategy: str, backend: str) -> pathlib.Path:
    return REPO_ROOT / "examples" / f"graphql-api-{strategy}-{backend}.yaml"


@pytest.mark.parametrize(("strategy", "backend"), CELLS)
async def test_cache_combination_scenario(strategy: str, backend: str) -> None:
    """Canonical Phase-2 acceptance scenario for one (strategy, backend) cell.

    Steps:
      1. GET getUser(id="3") -> miss + postgres + cache set.
      2. GET getUser(id="3") -> hit (response equality only; counter
         introspection skipped to keep this contract-level).
      3. updateUserName(id="3", name="renamed-<cell>") -> mutation runs;
         invalidator fires per strategy.
      4. GET getUser(id="3") again:
           - operation/tag: cache cleared, postgres returns the new name.
           - ttl: cache still serves the OLD name (mutation does NOT
             invalidate; time does - see test_ttl_strategy_eventually_expires).

    The mutation is rolled back via a second updateUserName in `finally` so
    re-running stays idempotent against the seed data.
    """
    yaml_path = _yaml_for_cell(strategy, backend)
    user_id = "3"
    new_name = f"renamed-{strategy}-{backend}"
    original_name = f"user-{user_id}"  # seed pattern: user-N for id N.

    async with _booted_app(yaml_path) as client:
        # 1. First read: cache miss, postgres serves, cache populated.
        first = await _gql(
            client, f'{{ getUser(id: "{user_id}") {{ id name }} }}'
        )
        assert first == {"getUser": {"id": user_id, "name": original_name}}

        # 2. Second read: cache hit (same response).
        second = await _gql(
            client, f'{{ getUser(id: "{user_id}") {{ id name }} }}'
        )
        assert second == {"getUser": {"id": user_id, "name": original_name}}

        try:
            # 3. Mutation: invalidator runs per strategy.
            mutated = await _gql(
                client,
                f'mutation {{ updateUserName(id: "{user_id}", '
                f'name: "{new_name}") {{ id name }} }}',
            )
            assert mutated == {
                "updateUserName": {"id": user_id, "name": new_name}
            }

            # 4. Next GET behavior is strategy-dependent.
            after = await _gql(
                client, f'{{ getUser(id: "{user_id}") {{ id name }} }}'
            )
            if strategy == "ttl":
                # ttl strategy: mutations do NOT explicitly invalidate; the
                # cached entry still has the OLD name even though postgres
                # holds the new one. This is the documented Phase-2 ttl
                # contract (roadmap section 6: time-based, not mutation-driven).
                assert after == {
                    "getUser": {"id": user_id, "name": original_name}
                }
            else:
                # operation / tag: cache cleared post-mutation; postgres
                # serves the new value through the now-empty cache.
                assert after == {
                    "getUser": {"id": user_id, "name": new_name}
                }
        finally:
            # Restore the seeded name so subsequent runs / cells stay
            # idempotent against the shared docker-compose postgres.
            await _gql(
                client,
                f'mutation {{ updateUserName(id: "{user_id}", '
                f'name: "{original_name}") {{ id name }} }}',
            )


SHORT_TTL_YAML = REPO_ROOT / "examples" / "graphql-api-ttl-1s-redis.yaml"


async def test_ttl_strategy_eventually_expires() -> None:
    """ttl strategy with ttl_seconds=1: post-mutation read after sleep > ttl misses.

    Complements the 6-cell matrix above, which only verifies the "cache stays
    stale right after mutation" half of the ttl contract. This test verifies
    the second half: once the TTL elapses, the next read misses and postgres
    serves the new value.

    Uses a dedicated short-TTL yaml (examples/graphql-api-ttl-1s-redis.yaml)
    rather than runtime mutation, because cacheProfiles are a load-time concern.
    """
    user_id = "4"
    new_name = "ttl-expiry-test"
    original_name = f"user-{user_id}"

    async with _booted_app(SHORT_TTL_YAML) as client:
        first = await _gql(
            client, f'{{ getUser(id: "{user_id}") {{ id name }} }}'
        )
        assert first == {"getUser": {"id": user_id, "name": original_name}}

        try:
            mutated = await _gql(
                client,
                f'mutation {{ updateUserName(id: "{user_id}", '
                f'name: "{new_name}") {{ id name }} }}',
            )
            assert mutated == {
                "updateUserName": {"id": user_id, "name": new_name}
            }

            # Wait past the 1s profile TTL with margin (Redis TTL is
            # millisecond-resolution but coarse-grained at second TTLs).
            await asyncio.sleep(1.5)

            after_expiry = await _gql(
                client, f'{{ getUser(id: "{user_id}") {{ id name }} }}'
            )
            assert after_expiry == {
                "getUser": {"id": user_id, "name": new_name}
            }
        finally:
            await _gql(
                client,
                f'mutation {{ updateUserName(id: "{user_id}", '
                f'name: "{original_name}") {{ id name }} }}',
            )
