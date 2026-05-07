"""Phase 0 smoke test: /stats returns the booting payload in-process."""
import httpx
import pytest

from framework.cli import app


@pytest.mark.asyncio
async def test_stats_endpoint_returns_booting_payload() -> None:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/stats")
    assert response.status_code == 200
    assert response.json() == {"status": "booting"}
