from __future__ import annotations

import httpx
import pytest
import pytest_bazel

from x.google_mcp_server.server import create_app


@pytest.mark.anyio
async def test_create_app_has_public_health_and_protected_gmail_and_calendar_mcp() -> None:
    app = create_app(google_access_token="test-google-access-token", bearer="test-bearer")
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client,
    ):
        assert (await client.get("/healthz")).status_code == 200

        for path in ("/gmail/mcp", "/calendar/mcp"):
            assert (await client.post(path, json={"jsonrpc": "2.0"})).status_code == 401
            response = await client.post(
                path,
                headers={"Authorization": "Bearer test-bearer"},
                json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            )
            assert response.status_code != 401


if __name__ == "__main__":
    pytest_bazel.main()
