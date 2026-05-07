import logging
import os
import pathlib
from collections.abc import AsyncIterator, Awaitable, Callable, MutableMapping
from contextlib import asynccontextmanager
from typing import Any, cast

import asyncpg  # type: ignore[import-untyped]
import redis.asyncio as redis_async
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route

from framework.cache.backend import CacheBackend, CacheCounters
from framework.cache.memory_backend import MemoryBackend
from framework.cache.redis_backend import RedisBackend
from framework.dsl.loader import load
from framework.graphql.server import build_graphql_app

logger = logging.getLogger(__name__)

_state: dict[str, Any] = {}


async def stats(_request: Request) -> JSONResponse:
    return JSONResponse({"status": "booting"})


async def _graphql_dispatch(
    scope: MutableMapping[str, Any],
    receive: Callable[[], Awaitable[MutableMapping[str, Any]]],
    send: Callable[[MutableMapping[str, Any]], Awaitable[None]],
) -> None:
    """Lazy ASGI dispatcher: defers to the lifespan-built GraphQL app."""
    graphql_app = _state.get("graphql_app")
    if graphql_app is None:
        # Boot not finished. Return 503 so callers can retry.
        if scope["type"] == "http":
            await send({
                "type": "http.response.start",
                "status": 503,
                "headers": [(b"content-type", b"application/json")],
            })
            await send({
                "type": "http.response.body",
                "body": b'{"error":"booting"}',
            })
        return
    await graphql_app(scope, receive, send)


@asynccontextmanager
async def _lifespan(_app: Starlette) -> AsyncIterator[None]:
    schema_path = pathlib.Path(
        os.environ.get("SCHEMA_PATH", "/app/examples/schema.graphql")
    )
    dsl_path = pathlib.Path(
        os.environ.get("DSL_PATH", "/app/examples/graphql-api-no-cache.yaml")
    )
    registry = load(dsl_path, schema_path)

    dsn = os.environ["POSTGRES_DSN"]
    pool_min = int(os.environ.get("POSTGRES_POOL_MIN", "1"))
    pool_max = int(os.environ.get("POSTGRES_POOL_MAX", "10"))
    pool = await asyncpg.create_pool(dsn=dsn, min_size=pool_min, max_size=pool_max)
    if pool is None:
        raise RuntimeError("asyncpg.create_pool returned None")

    # Phase 2: instantiate cache backends per profile (Q8 conditional Redis).
    counters = CacheCounters()
    backends_by_profile: dict[str, CacheBackend] | None = None
    redis_client: redis_async.Redis | None = None

    if registry.cache_profiles:
        backends_by_profile = {}

        redis_profiles_present = any(
            p.backend == "redis" for p in registry.cache_profiles.values()
        )
        in_memory_profiles = {
            name: prof
            for name, prof in registry.cache_profiles.items()
            if prof.backend == "in_memory"
        }

        if redis_profiles_present:
            redis_url = os.environ.get("REDIS_URL", "redis://redis:6379/0")
            redis_client = cast(
                redis_async.Redis,
                redis_async.from_url(  # type: ignore[no-untyped-call]
                    redis_url, decode_responses=False
                ),
            )
            for name, prof in registry.cache_profiles.items():
                if prof.backend == "redis":
                    backends_by_profile[name] = RedisBackend(redis_client, counters)

        if in_memory_profiles:
            memory_backend = MemoryBackend(in_memory_profiles, counters)
            for name in in_memory_profiles:
                backends_by_profile[name] = memory_backend

        logger.info(
            "Phase 2 cache: %d profiles, redis=%s, in_memory=%s",
            len(registry.cache_profiles),
            redis_profiles_present,
            bool(in_memory_profiles),
        )

    sdl = schema_path.read_text()
    graphql_app = build_graphql_app(
        sdl, registry, pool, backends_by_profile=backends_by_profile
    )

    _state["pool"] = pool
    _state["graphql_app"] = graphql_app
    _state["counters"] = counters
    _state["backends_by_profile"] = backends_by_profile
    _state["redis_client"] = redis_client
    logger.info(
        "framework boot complete: %d queries, %d mutations",
        len(registry.queries),
        len(registry.mutations),
    )

    try:
        yield
    finally:
        existing_pool = _state.pop("pool", None)
        if existing_pool is not None:
            await existing_pool.close()
        existing_redis: redis_async.Redis | None = _state.pop("redis_client", None)
        if existing_redis is not None:
            await existing_redis.aclose()
        _state.pop("graphql_app", None)
        _state.pop("counters", None)
        _state.pop("backends_by_profile", None)


app = Starlette(
    routes=[
        Route("/stats", stats, methods=["GET"]),
        Mount("/graphql", _graphql_dispatch),
    ],
    lifespan=_lifespan,
)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=4000)
