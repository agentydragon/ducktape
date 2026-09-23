"""The proxy's forwarding and validation, over the real projection it reads a thread's scope from.

Electric itself is a `MockTransport`, because what these assert is the request the proxy builds and
what it refuses to build. The scope behind it is real: the epoch a shape is pinned to is whatever the
fold materialized, and a faked resolver could drift from it without any test noticing.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Iterator
from contextlib import suppress
from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
import pytest_bazel
from fastapi import FastAPI, Request
from starlette.requests import ClientDisconnect
from starlette.types import Message
from testcontainers.postgres import PostgresContainer

from agentplane.app.agent_runtime.events.event_log import EventLogStore
from agentplane.app.agent_runtime.ingestion import Ingestion
from agentplane.app.agent_runtime.view.content import ContentStore, ThreadScope
from agentplane.app.conftest import migrated_database
from agentplane.app.electric import SUBSET_BODY_LIMIT, SUBSET_ROW_LIMIT, ElectricProxy, router
from agentplane.app.testing.replication_source import SANDBOX, SESSION, ReplicationSource
from agentplane.protocol import event_pb2

# The generated protocol stubs' own stub chain, which the mypy aspect resolves for direct deps only.
# gazelle:include_dep @pypi//protobuf


@dataclass(frozen=True)
class Seeded:
    thread: UUID
    scope: ThreadScope


# Every case here reads: the proxy builds a request and forwards or refuses it, and none writes. The
# expensive part of a per-test database is creating and migrating it, and that helper is
# synchronous, so the module can share one without a module-scoped event loop the async fixtures
# would then need. Each case still seeds its own sandbox, so they share no thread and no lease.
@pytest.fixture(scope="module")
def db_url(postgres_container: PostgresContainer) -> Iterator[str]:
    yield from migrated_database(postgres_container, "test_electric")


@pytest.fixture
async def seeded(event_logs: EventLogStore, content: ContentStore, ingestion: Ingestion) -> Seeded:
    sandbox = f"{SANDBOX}-{uuid4().hex[:8]}"
    source = ReplicationSource()
    for index in range(3):
        item_id = f"item-{index}"
        source.append(
            event_pb2.Event(
                item_started=event_pb2.ItemStarted(item_id=item_id, kind=event_pb2.ITEM_KIND_ASSISTANT_TEXT)
            )
        )
        source.append(event_pb2.Event(text_delta=event_pb2.TextDelta(item_id=item_id, text=f"body {index}")))
    thread = await event_logs.open(sandbox, SESSION, source.attached.spec)
    lease = await ingestion.acquire(sandbox, timedelta(minutes=2))
    assert lease is not None
    await ingestion.set_attached(thread, source.attached, lease=lease)
    await ingestion.record(thread, source.entries, lease=lease)
    scope = await content.current_scope(thread)
    assert scope is not None
    return Seeded(thread, scope)


def make_app(upstream: httpx.MockTransport, content: ContentStore) -> tuple[FastAPI, httpx.AsyncClient]:
    electric = httpx.AsyncClient(transport=upstream, base_url="http://electric")
    app = FastAPI()
    app.state.electric = ElectricProxy(electric, content)
    app.include_router(router)
    return app, electric


async def _unexpected(_request: httpx.Request) -> httpx.Response:
    raise AssertionError("rejected requests must not reach Electric")


def _pinned(request: httpx.Request) -> dict[str, str]:
    """The part of a forwarded query that decides which rows a shape holds."""
    query = httpx.QueryParams(request.url.query)
    return {key: value for key, value in query.multi_items() if key in {"table", "where"} or key.startswith("params[")}


async def test_scope_names_the_epoch_and_how_far_the_fold_has_applied(seeded: Seeded, content: ContentStore) -> None:
    app, electric = make_app(httpx.MockTransport(_unexpected), content)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://app") as client:
        scope = await client.get(f"/threads/{seeded.thread}/sync/scope")
        unknown = await client.get(f"/threads/{UUID(int=0)}/sync/scope")
    await electric.aclose()

    assert scope.json() == {
        "projection_epoch": seeded.scope.projection_epoch,
        "through_cursor": str(seeded.scope.through_cursor),
    }
    assert unknown.status_code == 404


async def test_entity_shape_is_the_whole_thread_pinned_by_the_server(seeded: Seeded, content: ContentStore) -> None:
    seen: list[httpx.Request] = []

    async def upstream(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            stream=httpx.ByteStream(b'[{"headers":{"control":"up-to-date"}}]'),
            headers={"electric-offset": "7_0", "electric-up-to-date": "true", "x-private": "no"},
        )

    app, electric = make_app(httpx.MockTransport(upstream), content)
    # Everything Electric's client sends, including what it adds when recovering an expired handle and
    # when it follows a shape over SSE.
    protocol = {
        "offset": "now",
        "handle": "h",
        "live": "true",
        "live_sse": "true",
        "experimental_live_sse": "true",
        "cursor": "c",
        "log": "changes_only",
        "expired_handle": "old",
        "cache-buster": "b",
    }
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://app") as client:
        response = await client.get(
            f"/threads/{seeded.thread}/sync/entities",
            params={"projection_epoch": seeded.scope.projection_epoch, **protocol},
            headers={"electric-protocol-version": "1.0"},
        )
    await electric.aclose()

    assert response.status_code == 200
    assert response.headers["electric-offset"] == "7_0"
    assert response.headers["electric-up-to-date"] == "true"
    assert response.headers["cache-control"] == "private, no-cache"
    assert "x-private" not in response.headers
    [forwarded] = seen
    assert forwarded.method == "GET"
    assert forwarded.headers["electric-protocol-version"] == "1.0"
    assert _pinned(forwarded) == {
        "table": "thread_entity",
        "where": "thread_id = $1 AND projection_epoch = $2",
        "params[1]": str(seeded.thread),
        "params[2]": seeded.scope.projection_epoch,
    }
    query = httpx.QueryParams(forwarded.url.query)
    # Rows are mutable: a reader bootstraps from subsets of current state, never a replayed log.
    assert query["log"] == "changes_only"
    assert query["replica"] == "full"
    assert query["queryable_columns"] == query["columns"]
    assert "projection_epoch" not in query
    assert {key: query[key] for key in protocol} == protocol


async def test_a_re_read_revalidates_and_relays_electric_s_not_modified(seeded: Seeded, content: ContentStore) -> None:
    """A served offset is cached and revalidated, never re-transferred and never served unchecked."""
    seen: list[httpx.Request] = []

    async def upstream(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.headers.get("if-none-match") == '"shape-7_0"':
            return httpx.Response(304, stream=httpx.ByteStream(b""), headers={"etag": '"shape-7_0"'})
        return httpx.Response(200, stream=httpx.ByteStream(b"[]"), headers={"etag": '"shape-7_0"'})

    app, electric = make_app(httpx.MockTransport(upstream), content)
    params = {"projection_epoch": seeded.scope.projection_epoch, "offset": "7_0", "handle": "h"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://app") as client:
        path = f"/threads/{seeded.thread}/sync/entities"
        first = await client.get(path, params=params)
        again = await client.get(path, params=params, headers={"if-none-match": first.headers["etag"]})
    await electric.aclose()

    assert first.status_code == 200
    assert first.headers["etag"] == '"shape-7_0"'
    # Stored, but only ever served through a request this proxy authorized: `no-store` would make
    # the entity tag inert, and a `max-age` would let a cached body outlive the caller's session.
    assert first.headers["cache-control"] == "private, no-cache"
    assert again.status_code == 304
    assert again.content == b""
    assert [request.headers.get("if-none-match") for request in seen] == [None, '"shape-7_0"']


@pytest.mark.parametrize(
    "query",
    [
        "offset=now&table=event",
        "offset=now&log=full",
        "offset=now&offset=-1",
        "offset=now&queryable_columns=state",
        # A subset rides in a POST body, where its form is checked; never in the query.
        "offset=0_0&handle=h&subset__where=true",
        "offset=0_0&handle=h&subset__limit=1",
    ],
)
async def test_shape_requests_outside_the_protocol_are_rejected(
    query: str, seeded: Seeded, content: ContentStore
) -> None:
    app, electric = make_app(httpx.MockTransport(_unexpected), content)
    epoch = httpx.QueryParams({"projection_epoch": seeded.scope.projection_epoch})
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://app") as client:
        response = await client.get(f"/threads/{seeded.thread}/sync/entities?{epoch}&{query}")
    await electric.aclose()
    assert response.status_code == 400


async def test_a_retired_epoch_or_a_thread_without_a_fold_is_gone(seeded: Seeded, content: ContentStore) -> None:
    """A rebuilt fold is a new epoch, and a reader holding the old one re-resolves its scope."""
    app, electric = make_app(httpx.MockTransport(_unexpected), content)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://app") as client:
        retired = await client.get(
            f"/threads/{seeded.thread}/sync/entities", params={"projection_epoch": "retired", "offset": "now"}
        )
        unfolded = await client.post(
            f"/threads/{UUID(int=0)}/sync/chunks/text",
            params={"projection_epoch": seeded.scope.projection_epoch, "offset": "0_0", "handle": "h"},
            json=_bodies(1),
        )
    await electric.aclose()
    assert (retired.status_code, unfolded.status_code) == (410, 410)


@pytest.mark.parametrize(
    "subset",
    [
        {"order_by": "entity_index DESC", "limit": 60},
        {
            "where": "entity_index < $1",
            "params": {"1": "40"},
            "order_by": "entity_index DESC",
            "limit": SUBSET_ROW_LIMIT,
        },
        {"where": "entity_kind = 'view_state'", "order_by": "entity_index DESC", "limit": 1},
        {"where": "entity_kind = 'command' AND pending = true", "order_by": "entity_index DESC", "limit": 200},
        {
            "where": "entity_kind = 'command' AND entity_id = ANY($1)",
            "params": {"1": '{"a","b"}'},
            "order_by": "entity_index DESC",
            "limit": 2,
        },
    ],
)
async def test_entity_subsets_forward_within_the_thread_s_shape(
    subset: dict[str, Any], seeded: Seeded, content: ContentStore
) -> None:
    seen: list[httpx.Request] = []

    async def upstream(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, stream=httpx.ByteStream(b'{"data":[],"metadata":{}}'))

    app, electric = make_app(httpx.MockTransport(upstream), content)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://app") as client:
        response = await client.post(
            f"/threads/{seeded.thread}/sync/entities",
            params={"projection_epoch": seeded.scope.projection_epoch, "offset": "0_0", "handle": "h"},
            json=subset,
        )
    await electric.aclose()

    assert response.status_code == 200
    [forwarded] = seen
    assert forwarded.method == "POST"
    # Electric ANDs a subset with the shape's own predicate, which the server still pins.
    assert _pinned(forwarded) == {
        "table": "thread_entity",
        "where": "thread_id = $1 AND projection_epoch = $2",
        "params[1]": str(seeded.thread),
        "params[2]": seeded.scope.projection_epoch,
    }
    assert json.loads(forwarded.content) == subset


@pytest.mark.parametrize(
    ("subset", "expected"),
    [
        ({"where": "true", "order_by": "entity_index DESC", "limit": 1}, 400),
        ({"where": "entity_kind = 'turn'", "order_by": "entity_index DESC", "limit": 1}, 400),
        ({"where": "entity_kind = 'view_state'", "limit": 1}, 400),
        ({"order_by": "entity_index DESC"}, 400),
        ({"order_by": "entity_index DESC", "limit": 0}, 400),
        ({"order_by": "entity_index DESC", "limit": SUBSET_ROW_LIMIT + 1}, 400),
        ({"order_by": "cursor DESC", "limit": 1}, 400),
        ({"order_by": "entity_index DESC", "limit": 1, "params": {"1": "x"}}, 400),
        (
            {"where": "entity_index < $1", "params": {"1": "1 OR true"}, "order_by": "entity_index DESC", "limit": 1},
            400,
        ),
        ({"where": "entity_index < $1", "order_by": "entity_index DESC", "limit": 1}, 400),
        (
            {"where": "entity_kind = 'command' AND entity_id = ANY($1)", "order_by": "entity_index DESC", "limit": 1},
            400,
        ),
        ({"order_by": "entity_index DESC", "limit": 1, "offset": 5}, 422),
    ],
)
async def test_entity_subsets_outside_the_forms_are_rejected(
    subset: dict[str, Any], expected: int, seeded: Seeded, content: ContentStore
) -> None:
    app, electric = make_app(httpx.MockTransport(_unexpected), content)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://app") as client:
        response = await client.post(
            f"/threads/{seeded.thread}/sync/entities",
            params={"projection_epoch": seeded.scope.projection_epoch, "offset": "0_0", "handle": "h"},
            json=subset,
        )
    await electric.aclose()
    assert response.status_code == expected


def _bodies(count: int) -> dict[str, Any]:
    return {
        "where": " OR ".join(f"(owner_id = ${2 * n - 1} AND generation = ${2 * n})" for n in range(1, count + 1)),
        "params": {
            key: value
            for n in range(1, count + 1)
            for key, value in ((str(2 * n - 1), f"item-{n}"), (str(2 * n), str(10 + n)))
        },
    }


async def test_chunk_shape_and_body_subsets_are_pinned_to_one_field(seeded: Seeded, content: ContentStore) -> None:
    seen: list[httpx.Request] = []

    async def upstream(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, stream=httpx.ByteStream(b"[]"))

    app, electric = make_app(httpx.MockTransport(upstream), content)
    params = {"projection_epoch": seeded.scope.projection_epoch, "offset": "0_0", "handle": "h"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://app") as client:
        path = f"/threads/{seeded.thread}/sync/chunks/text"
        shape = await client.get(path, params=params)
        one = await client.post(path, params=params, json=_bodies(1))
        most = await client.post(path, params=params, json=_bodies(SUBSET_BODY_LIMIT))
        unknown_field = await client.get(f"/threads/{seeded.thread}/sync/chunks/secret", params=params)
    await electric.aclose()

    assert (shape.status_code, one.status_code, most.status_code) == (200, 200, 200)
    assert unknown_field.status_code == 422
    assert [request.method for request in seen] == ["GET", "POST", "POST"]
    for forwarded in seen:
        assert _pinned(forwarded) == {
            "table": "thread_payload_chunk",
            "where": "thread_id = $1 AND projection_epoch = $2 AND field = $3",
            "params[1]": str(seeded.thread),
            "params[2]": seeded.scope.projection_epoch,
            "params[3]": "text",
        }
    assert json.loads(seen[1].content) == _bodies(1)


@pytest.mark.parametrize(
    "subset",
    [
        {"where": "(owner_id = $1 AND generation = $2) OR true", "params": {"1": "a", "2": "1"}},
        {"where": "(generation = $2 AND owner_id = $1)", "params": {"1": "a", "2": "1"}},
        {
            "where": "(owner_id = $1 AND generation = $2) OR (owner_id = $4 AND generation = $3)",
            "params": {"1": "a", "2": "1", "3": "2", "4": "b"},
        },
        {"where": "(owner_id = $1 AND generation = $2)", "params": {"1": "a", "2": "1 OR true"}},
        {"where": "(owner_id = $1 AND generation = $2)", "params": {"1": "a"}},
        {"where": "(owner_id = $1 AND generation = $2)", "params": {"1": "a", "2": "1", "3": "b"}},
        _bodies(1) | {"limit": 1},
        _bodies(1) | {"order_by": "chunk_index"},
        _bodies(SUBSET_BODY_LIMIT + 1),
        {"where": "owner_id = ANY($1)", "params": {"1": "{a}"}},
        {},
    ],
)
async def test_body_subsets_name_only_owner_generation_pairs(
    subset: dict[str, Any], seeded: Seeded, content: ContentStore
) -> None:
    app, electric = make_app(httpx.MockTransport(_unexpected), content)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://app") as client:
        response = await client.post(
            f"/threads/{seeded.thread}/sync/chunks/text",
            params={"projection_epoch": seeded.scope.projection_epoch, "offset": "0_0", "handle": "h"},
            json=subset,
        )
    await electric.aclose()
    assert response.status_code == 400


@pytest.mark.parametrize("disconnect", ["cancel", "send", "receive"])
async def test_slow_downstream_bounds_upstream_reads_and_disconnect_closes_response(
    disconnect: str, seeded: Seeded, content: ContentStore
) -> None:
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

    app, electric = make_app(httpx.MockTransport(upstream), content)
    scope = {
        "type": "http",
        "asgi": {"spec_version": "2.3" if disconnect == "receive" else "2.4"},
        "query_string": b"offset=now",
        "headers": [],
    }
    response = await app.state.electric.entities(
        Request(scope), thread_id=seeded.thread, projection_epoch=seeded.scope.projection_epoch
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
