import logging
import os
import pathlib
from collections.abc import AsyncIterator, Awaitable, Callable, MutableMapping
from contextlib import asynccontextmanager
from typing import Any

import asyncpg  # type: ignore[import-untyped]
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route

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

    sdl = schema_path.read_text()
    graphql_app = build_graphql_app(sdl, registry, pool)

    _state["pool"] = pool
    _state["graphql_app"] = graphql_app
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
        _state.pop("graphql_app", None)


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
