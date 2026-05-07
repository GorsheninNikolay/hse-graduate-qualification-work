"""Test fixtures.

Phase 0 strategy: fixtures yield URL strings only. They do NOT open connections.
Phase 1+ tests will run against the live `make up` stack on localhost.
"""
import os
import pytest


@pytest.fixture(scope="session")
def postgres_dsn() -> str:
    """Postgres DSN. Defaults to the docker-compose-exposed instance."""
    return os.environ.get(
        "POSTGRES_DSN",
        "postgresql://postgres:postgres@localhost:5432/postgres",
    )


@pytest.fixture(scope="session")
def redis_url() -> str:
    """Redis URL. Defaults to the docker-compose-exposed instance."""
    return os.environ.get(
        "REDIS_URL",
        "redis://localhost:6379/0",
    )
