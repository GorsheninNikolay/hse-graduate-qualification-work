"""Phase 3 smoke test: /stats returns the counter-payload schema in-process.

Pre-lifespan (no DB connection), the framework returns a zero-valued payload
with the same 6 keys; framework/cli.py:stats() handles the pre-lifespan
branch with backend="none" + zero counters.
"""
import httpx
import pytest

from framework.cli import app


@pytest.mark.asyncio
async def test_stats_endpoint_returns_counter_payload() -> None:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/stats")
    assert response.status_code == 200
    body = response.json()
    expected_keys = {
        "cache_hits", "cache_misses", "invalidations",
        "errors", "request_count_by_op", "backend",
    }
    assert set(body) == expected_keys, body
    assert body["backend"] in {"redis", "in_memory", "mixed", "none"}
    # Pre-lifespan path: counters are all zero.
    assert body["cache_hits"] == 0
    assert body["cache_misses"] == 0
    assert body["invalidations"] == 0
    assert body["request_count_by_op"] == {}
    assert body["errors"] == {}
