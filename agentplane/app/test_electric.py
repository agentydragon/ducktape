from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import suppress
from uuid import UUID

import httpx
import pytest
import pytest_bazel
from fastapi import FastAPI, Request
from starlette.requests import ClientDisconnect
from starlette.types import Message

from agentplane.app.electric import ElectricProxy, router
from agentplane.app.trajectory import ConversationEntityInterest, ConversationScope

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
    thread_id: UUID, owner_cursor: int, owner_id: str, field: str, generation: int
) -> ConversationScope | None:
    if (thread_id, owner_cursor, owner_id, field, generation) != (THREAD, 12, "item-1", "text", 2):
        return None
    return SCOPE


async def current_scope(thread_id: UUID) -> ConversationScope | None:
    return SCOPE if thread_id == THREAD else None


def make_app(upstream: httpx.MockTransport) -> tuple[FastAPI, httpx.AsyncClient]:
    electric = httpx.AsyncClient(transport=upstream, base_url="http://electric")
    app = FastAPI()
    app.state.electric = ElectricProxy(electric, entities, payload, current_scope)
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
            f"/threads/{THREAD}/sync/entities?source_id=runner%2Fsource&projection_epoch=epoch-4&anchor_cursor=99&tail_from=70&offset=now&live=false&cursor=cache&log=changes_only"
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
    assert query["log"] == "changes_only"
    assert query["queryable_columns"] == query["columns"]
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
        stale_tail = await client.get(
            f"/threads/{THREAD}/sync/entities?source_id=runner%2Fsource&projection_epoch=epoch-4&anchor_cursor=99&tail_from=0&offset=-1"
        )
        arbitrary = await client.get(
            f"/threads/{THREAD}/sync/entities?source_id=runner%2Fsource&projection_epoch=epoch-4&anchor_cursor=99&tail_from=70&table=event"
        )
        bad_log = await client.get(
            f"/threads/{THREAD}/sync/entities?source_id=runner%2Fsource&projection_epoch=epoch-4&anchor_cursor=99&tail_from=70&log=full"
        )
    await electric.aclose()

    assert stale_tail.status_code == 410
    assert arbitrary.status_code == 400
    assert bad_log.status_code == 400


@pytest.mark.parametrize("field", ["source_id", "projection_epoch"])
async def test_old_entity_scope_is_rejected_before_forwarding(field: str) -> None:
    async def unexpected(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("retired scopes must not reach Electric")

    app, electric = make_app(httpx.MockTransport(unexpected))
    params = {
        "source_id": SCOPE.source_id,
        "projection_epoch": SCOPE.projection_epoch,
        "anchor_cursor": "99",
        "tail_from": "70",
        field: "retired-scope",
    }
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://app") as client:
        response = await client.get(f"/threads/{THREAD}/sync/entities", params=params)
    await electric.aclose()
    assert response.status_code == 410


@pytest.mark.parametrize(
    "subset",
    [
        {"subset__where": "true = true"},
        {"subset__where": "TRUE = TRUE", "subset__params": "{}"},
        {"subset__where": "true = true", "subset__params": "[]"},
    ],
)
async def test_current_snapshot_cannot_change_fixed_shape(subset: dict[str, str]) -> None:
    seen: list[httpx.Request] = []

    async def upstream(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, stream=httpx.ByteStream(b"[]"))

    app, electric = make_app(httpx.MockTransport(upstream))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://app") as client:
        response = await client.get(
            f"/threads/{THREAD}/sync/entities",
            params={
                "source_id": SCOPE.source_id,
                "projection_epoch": SCOPE.projection_epoch,
                "anchor_cursor": "99",
                "tail_from": "70",
                "offset": "0_0",
                "handle": "fixed",
                **subset,
            },
            headers={"electric-protocol-version": "1.0"},
        )
    await electric.aclose()
    assert response.status_code == 200
    forwarded = seen[0].url.params
    assert forwarded["log"] == "changes_only"
    assert "cursor >= $4" in forwarded["where"]
    assert forwarded["params[4]"] == "70"
    assert seen[0].headers["electric-protocol-version"] == "1.0"
    for key, value in subset.items():
        assert forwarded[key] == value


@pytest.mark.parametrize(
    "query",
    [
        "subset__where=entity_id+%3D+'other'",
        "subset__params=%7B%221%22%3A%22other%22%7D",
        "subset__params=invalid-json",
        "subset__params=null",
        "subset__limit=1",
        "subset__offset=1",
        "subset__order_by=cursor",
        "subset__where=true+%3D+true&subset__where=false",
        "offset=now&offset=-1",
        "queryable_columns=state",
    ],
)
async def test_snapshot_rejects_caller_selection_and_duplicate_parameters(query: str) -> None:
    async def unexpected(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("rejected snapshots must not reach Electric")

    app, electric = make_app(httpx.MockTransport(unexpected))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://app") as client:
        response = await client.get(
            f"/threads/{THREAD}/sync/entities?source_id=runner%2Fsource&projection_epoch=epoch-4&anchor_cursor=99&tail_from=70&{query}"
        )
    await electric.aclose()
    assert response.status_code == 400


async def test_payload_shape_selects_a_whole_generation_and_no_revision_within_it() -> None:
    """A revision-bounded prefix would define a distinct shape per revision; the route refuses one."""
    seen: list[httpx.Request] = []

    async def upstream(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, stream=httpx.ByteStream(b"[]"))

    app, electric = make_app(httpx.MockTransport(upstream))
    generation = (
        "source_id=runner%2Fsource&projection_epoch=epoch-4&owner_cursor=12&owner_id=item-1&field=text&generation=2"
    )
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://app") as client:
        chunks = await client.get(f"/threads/{THREAD}/sync/payload-chunks?{generation}&offset=-1")
        narrowed = await client.get(f"/threads/{THREAD}/sync/payload-chunks?{generation}&offset=-1&revision_cursor=18")
        missing = await client.get(
            f"/threads/{THREAD}/sync/payload-chunks?{generation.replace('generation=2', 'generation=3')}"
        )
    await electric.aclose()

    assert chunks.status_code == 200
    assert narrowed.status_code == 400
    assert missing.status_code == 410
    forwarded = httpx.QueryParams(seen[0].url.query)
    assert [request.url for request in seen] == [seen[0].url]
    assert forwarded["table"] == "conversation_payload_chunk"
    assert "chunk_index" not in forwarded["where"]
    assert "params[8]" not in forwarded


async def test_command_shape_is_scoped_bounded_and_includes_settled_or_future_ids() -> None:
    seen: list[httpx.Request] = []

    async def upstream(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, stream=httpx.ByteStream(b"[]"))

    app, electric = make_app(httpx.MockTransport(upstream))
    params = [("source_id", SCOPE.source_id), ("projection_epoch", SCOPE.projection_epoch), ("offset", "-1")]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://app") as client:
        response = await client.get(
            f"/threads/{THREAD}/sync/commands",
            params=[*params, ("command_id", "future"), ("command_id", "failed"), ("command_id", "failed")],
        )
        assert response.status_code == 200
        for invalid in [
            params,
            [*params, ("command_id", "")],
            [*params, *[("command_id", str(n)) for n in range(129)]],
        ]:
            assert (await client.get(f"/threads/{THREAD}/sync/commands", params=tuple(invalid))).status_code == 422
        stale = [(key, "old" if key == "projection_epoch" else value) for key, value in params]
        assert (
            await client.get(f"/threads/{THREAD}/sync/commands", params=[*stale, ("command_id", "failed")])
        ).status_code == 410
        assert (
            await client.get(f"/threads/{UUID(int=0)}/sync/commands", params=[*params, ("command_id", "failed")])
        ).status_code == 410
    await electric.aclose()
    assert len(seen) == 1
    query = httpx.QueryParams(seen[0].url.query)
    assert query["where"] == (
        "thread_id = $1 AND source_id = $2 AND projection_epoch = $3 AND "
        "entity_kind = 'command' AND entity_id IN ($4,$5)"
    )
    assert query["params[1]"] == str(THREAD)
    assert query["params[2]"] == SCOPE.source_id
    assert query["params[3]"] == SCOPE.projection_epoch
    assert query["params[4]"] == "failed"
    assert query["params[5]"] == "future"
    assert "command_id" not in query


@pytest.mark.parametrize("disconnect", ["cancel", "send", "receive"])
async def test_slow_downstream_bounds_upstream_reads_and_disconnect_closes_response(disconnect: str) -> None:
    class Chunks(httpx.AsyncByteStream):
        def __init__(self) -> None:
            self.reads = 0
            self.closed = False

        async def __aiter__(self) -> AsyncIterator[bytes]:
            for _ in range(10_000):
                self.reads += 1
                yield b"x" * 8192

        async def aclose(self) -> None:
            self.closed = True

    chunks = Chunks()

    async def upstream(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=chunks)

    app, electric = make_app(httpx.MockTransport(upstream))
    scope = {
        "type": "http",
        "asgi": {"spec_version": "2.3" if disconnect == "receive" else "2.4"},
        "query_string": b"offset=-1",
        "headers": [],
    }
    response = await app.state.electric.entities(
        Request(scope),
        thread_id=THREAD,
        source_id=SCOPE.source_id,
        projection_epoch=SCOPE.projection_epoch,
        anchor_cursor=99,
        tail_from=70,
        window_from=None,
        window_before=None,
    )
    first = asyncio.Event()
    release = asyncio.Event()
    second = asyncio.Event()
    blocked = asyncio.Event()
    disconnected = asyncio.Event()

    async def send(message: Message) -> None:
        if message["type"] != "http.response.body":
            return
        if not first.is_set():
            first.set()
            await release.wait()
        else:
            second.set()
            await blocked.wait()
            raise OSError("downstream disconnected")

    async def receive() -> Message:
        assert disconnect == "receive"
        await disconnected.wait()
        return {"type": "http.disconnect"}

    delivery = asyncio.create_task(response(scope, receive, send))
    try:
        async with asyncio.timeout(10):
            await first.wait()
            assert chunks.reads == 1
            release.set()
            await second.wait()
            assert chunks.reads == 2
            if disconnect == "cancel":
                delivery.cancel()
            elif disconnect == "send":
                blocked.set()
            else:
                disconnected.set()
            with suppress(asyncio.CancelledError, ClientDisconnect):
                await delivery
    finally:
        delivery.cancel()
        with suppress(asyncio.CancelledError, ClientDisconnect):
            await delivery
        await electric.aclose()
    assert chunks.closed
    assert chunks.reads == 2


if __name__ == "__main__":
    pytest_bazel.main()
