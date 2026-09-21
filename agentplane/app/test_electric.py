from __future__ import annotations

from uuid import UUID

import httpx
import pytest_bazel
from fastapi import FastAPI

from agentplane.app.electric import ElectricProxy, router
from agentplane.app.trajectory import ConversationEntityInterest, ConversationPayloadSelection, ConversationScope

THREAD = UUID("00000000-0000-0000-0000-000000000123")
SCOPE = ConversationScope(source_id="runner/source", projection_epoch="epoch-4", through_cursor=99)


async def entities(
    thread_id: UUID, anchor_cursor: int | None, before_cursor: int | None, page_size: int
) -> ConversationEntityInterest | None:
    assert thread_id == THREAD
    assert page_size == 30
    return ConversationEntityInterest(
        SCOPE,
        99 if anchor_cursor is None else anchor_cursor,
        70,
        10 if before_cursor is not None else None,
        before_cursor,
    )


async def payload(
    thread_id: UUID, owner_cursor: int, owner_id: str, field: str, generation: int, revision_cursor: int
) -> ConversationPayloadSelection | None:
    if (thread_id, owner_cursor, owner_id, field, generation, revision_cursor) != (THREAD, 12, "item-1", "text", 2, 18):
        return None
    return ConversationPayloadSelection(SCOPE, 12, "item-1", "text", 2, 18, True, 3, 17)


def make_app(upstream: httpx.MockTransport) -> tuple[FastAPI, httpx.AsyncClient]:
    electric = httpx.AsyncClient(transport=upstream, base_url="http://electric")
    app = FastAPI()
    app.state.electric = ElectricProxy(electric, entities, payload)
    app.include_router(router)
    return app, electric


async def test_entity_shape_is_bounded_and_fixed_by_server() -> None:
    seen: httpx.Request | None = None

    async def upstream(request: httpx.Request) -> httpx.Response:
        nonlocal seen
        seen = request
        return httpx.Response(
            200,
            stream=httpx.ByteStream(b'[{"headers":{"control":"up-to-date"}}]'),
            headers={"electric-offset": "7_0", "electric-up-to-date": "true", "x-private": "no"},
        )

    app, electric = make_app(httpx.MockTransport(upstream))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://app") as client:
        response = await client.get(
            f"/threads/{THREAD}/sync/entities?anchor_cursor=99&tail_from=70&offset=now&live=false&cursor=cache"
        )
    await electric.aclose()

    assert response.status_code == 200
    assert response.headers["electric-offset"] == "7_0"
    assert response.headers["electric-up-to-date"] == "true"
    assert response.headers["cache-control"] == "private, no-store"
    assert "x-private" not in response.headers
    assert seen is not None
    query = httpx.QueryParams(seen.url.query)
    assert query["table"] == "conversation_entity"
    assert "log" not in query
    assert query["replica"] == "full"
    assert "cursor >= $4" in query["where"]
    assert {str(index): query[f"params[{index}]"] for index in range(1, 5)} == {
        "1": str(THREAD),
        "2": "runner/source",
        "3": "epoch-4",
        "4": "70",
    }


async def test_stale_or_client_widened_interest_is_rejected() -> None:
    async def unexpected(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("rejected requests must not reach Electric")

    app, electric = make_app(httpx.MockTransport(unexpected))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://app") as client:
        stale_tail = await client.get(f"/threads/{THREAD}/sync/entities?anchor_cursor=99&tail_from=0&offset=-1")
        arbitrary = await client.get(f"/threads/{THREAD}/sync/entities?anchor_cursor=99&tail_from=70&table=event")
    await electric.aclose()

    assert stale_tail.status_code == 409
    assert arbitrary.status_code == 400


async def test_payload_shape_uses_server_verified_exact_revision() -> None:
    seen: httpx.Request | None = None

    async def upstream(request: httpx.Request) -> httpx.Response:
        nonlocal seen
        seen = request
        return httpx.Response(200, stream=httpx.ByteStream(b"[]"))

    app, electric = make_app(httpx.MockTransport(upstream))
    query = (
        "source_id=runner%2Fsource&projection_epoch=epoch-4&owner_cursor=12&owner_id=item-1&field=text"
        "&generation=2&revision_cursor=18&offset=-1"
    )
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://app") as client:
        interest = await client.get(f"/threads/{THREAD}/sync/payload-interest?{query}")
        chunks = await client.get(f"/threads/{THREAD}/sync/payload-chunks?{query}")
        missing = await client.get(
            f"/threads/{THREAD}/sync/payload-interest?source_id=runner%2Fsource&projection_epoch=epoch-4"
            "&owner_cursor=12&owner_id=item-1&field=text&generation=2&revision_cursor=19"
        )
    await electric.aclose()

    assert interest.json()["chunk_count"] == "3"
    assert chunks.status_code == 200
    assert missing.status_code == 410
    assert seen is not None
    forwarded = httpx.QueryParams(seen.url.query)
    assert forwarded["table"] == "conversation_payload_chunk"
    assert "chunk_index < $8" in forwarded["where"]
    assert forwarded["params[8]"] == "3"


if __name__ == "__main__":
    pytest_bazel.main()
