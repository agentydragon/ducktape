from __future__ import annotations

import json
from uuid import UUID

import httpx
import pytest_bazel
from fastapi import FastAPI

from agentplane.app.electric import ConversationScope, ElectricProxy, router

THREAD = UUID("00000000-0000-0000-0000-000000000123")


async def test_shape_definition_is_fixed_by_server() -> None:
    seen: httpx.Request | None = None

    async def upstream(request: httpx.Request) -> httpx.Response:
        nonlocal seen
        seen = request
        return httpx.Response(
            200,
            content=b'[{"headers":{"control":"up-to-date"}}]',
            headers={"electric-offset": "7_0", "x-private": "no"},
        )

    async def scope(thread_id: UUID) -> ConversationScope | None:
        return ConversationScope(thread_id=thread_id, source_id="runner/source", projection_epoch=4)

    upstream_client = httpx.AsyncClient(transport=httpx.MockTransport(upstream), base_url="http://electric")
    app = FastAPI()
    app.state.electric = ElectricProxy(upstream_client, scope)
    app.include_router(router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://app") as client:
        response = await client.get(f"/threads/{THREAD}/sync/entities?offset=now&live=false")
    await upstream_client.aclose()

    assert response.status_code == 200
    assert response.headers["electric-offset"] == "7_0"
    assert "x-private" not in response.headers
    assert seen is not None
    query = httpx.QueryParams(seen.url.query)
    assert query["table"] == "conversation_entity"
    assert query["log"] == "changes_only"
    assert query["replica"] == "full"
    assert json.loads(query["params"]) == {"1": str(THREAD), "2": "runner/source", "3": 4}


async def test_client_cannot_override_shape_or_issue_arbitrary_subset() -> None:
    async def unexpected(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("rejected requests must not reach Electric")

    async def scope(thread_id: UUID) -> ConversationScope | None:
        return ConversationScope(thread_id=thread_id, source_id="source", projection_epoch=1)

    upstream_client = httpx.AsyncClient(transport=httpx.MockTransport(unexpected), base_url="http://electric")
    app = FastAPI()
    app.state.electric = ElectricProxy(upstream_client, scope)
    app.include_router(router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://app") as client:
        override = await client.get(f"/threads/{THREAD}/sync/entities?offset=-1&table=event")
        arbitrary = await client.post(
            f"/threads/{THREAD}/sync/entities?offset=0_0&handle=h",
            json={"where": "TRUE", "params": {}, "order_by": "segment_cursor DESC", "limit": 30},
        )
    await upstream_client.aclose()

    assert override.status_code == 400
    assert arbitrary.status_code == 422


if __name__ == "__main__":
    pytest_bazel.main()
