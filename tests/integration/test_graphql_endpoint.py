"""Integration tests for the /graphql endpoint backed by live postgres.

These tests run the Starlette app's lifespan in-process via httpx ASGITransport.
The lifespan opens an asyncpg pool against the docker-compose postgres exposed
on localhost:5432, so the test module SKIPS itself if the port is unreachable.

To run: `make up && sleep 8 && poetry run pytest tests/integration/`.
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


pytestmark = pytest.mark.skipif(
    not _postgres_reachable(),
    reason="postgres not reachable on 127.0.0.1:5432; run `make up` first",
)


# Env vars must be set BEFORE framework.cli is imported because _lifespan reads
# them at call time but the module-level Starlette app is constructed at import.
# Defaults match the docker-compose-exposed services.
os.environ.setdefault(
    "POSTGRES_DSN",
    "postgresql://postgres:postgres@localhost:5432/postgres",
)
os.environ.setdefault("SCHEMA_PATH", str(REPO_ROOT / "examples" / "schema.graphql"))
os.environ.setdefault(
    "DSL_PATH", str(REPO_ROOT / "examples" / "graphql-api-no-cache.yaml")
)

from framework.cli import app  # noqa: E402  (import after env-var defaults)


@asynccontextmanager
async def _client_with_lifespan() -> AsyncIterator[httpx.AsyncClient]:
    """Drive Starlette lifespan around an httpx ASGITransport client."""
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            yield client


async def _gql(client: httpx.AsyncClient, query: str) -> dict[str, Any]:
    # Trailing slash so Starlette's Mount doesn't emit a 307 redirect.
    response = await client.post("/graphql/", json={"query": query})
    assert response.status_code == 200, response.text
    body = response.json()
    assert "errors" not in body, body
    data = body["data"]
    assert isinstance(data, dict)
    return data


async def test_get_user_returns_seeded_row() -> None:
    async with _client_with_lifespan() as client:
        data = await _gql(client, '{ getUser(id: "1") { id name teamId } }')
    # GraphQL ID type coerces asyncpg BIGINT (int) -> string on the way out.
    assert data == {"getUser": {"id": "1", "name": "user-1", "teamId": "1"}}


async def test_list_users_by_team_returns_deterministic_set() -> None:
    async with _client_with_lifespan() as client:
        data = await _gql(
            client, '{ listUsersByTeam(teamId: "1") { id name } }'
        )
    users = data["listUsersByTeam"]
    assert isinstance(users, list)
    # seed.sql: 10000 users round-robin across 100 teams -> 100 users per team.
    assert len(users) == 100
    # user 1 is on team 1: ((1-1) % 100) + 1 == 1.
    assert {"id": "1", "name": "user-1"} in users


async def test_update_user_name_mutation_persists() -> None:
    async with _client_with_lifespan() as client:
        before = await _gql(client, '{ getUser(id: "2") { id name } }')
        original_name = before["getUser"]["name"]
        try:
            mutated = await _gql(
                client,
                'mutation { updateUserName(id: "2", name: "phase1-renamed") '
                "{ id name } }",
            )
            assert mutated == {
                "updateUserName": {"id": "2", "name": "phase1-renamed"}
            }
            after = await _gql(client, '{ getUser(id: "2") { id name } }')
            assert after["getUser"]["name"] == "phase1-renamed"
        finally:
            # Idempotent cleanup so re-running the test keeps a clean fixture.
            await _gql(
                client,
                f'mutation {{ updateUserName(id: "2", name: "{original_name}") '
                "{ id name } }",
            )
