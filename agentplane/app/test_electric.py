"""The proxy's forwarding and validation, over the real projection it reads its bounds from.

Electric itself is a `MockTransport`, because what these assert is the query the proxy builds and
what it refuses to build. The interest, scope and payload revision behind it are real: a faked
resolver can drift from `entity_interest` without any test noticing, and the bounds it
returns are exactly what the shape's identity is made of.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterator, Iterator
from contextlib import suppress
from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID, uuid4

import httpx
import pytest
import pytest_bazel
from fastapi import FastAPI, Request
from starlette.requests import ClientDisconnect
from starlette.types import Message
from testcontainers.postgres import PostgresContainer

from agentplane.app.conftest import migrated_database
from agentplane.app.electric import ElectricProxy, router
from agentplane.app.testing.replication_source import SANDBOX, SESSION, ReplicationSource
from agentplane.app.thread_fold import PayloadField
from agentplane.app.trajectory.store import ThreadEntityInterest, ThreadPayloadSelection, TrajectoryStore
from agentplane.app.trajectory.views import EntityKind
from agentplane.protocol import event_pb2

# The generated protocol stubs' own stub chain, which the mypy aspect resolves for direct deps only.
# gazelle:include_dep @pypi//protobuf

# Enough items that a latest-30 interest has segments on both sides of its bound.
_ITEMS = 35


@dataclass(frozen=True)
class Seeded:
    """One projected thread, and the identities the proxy's routes take as parameters."""

    thread: UUID
    interest: ThreadEntityInterest
    selection: ThreadPayloadSelection


# Every case here reads: the proxy builds a query and forwards or refuses it, and none writes. The
# expensive part of a per-test database is creating and migrating it, and that helper is
# synchronous, so the module can share one without a module-scoped event loop the async fixtures
# would then need. Each case still seeds its own sandbox, so they share no thread and no lease.
@pytest.fixture(scope="module")
def db_url(postgres_container: PostgresContainer) -> Iterator[str]:
    yield from migrated_database(postgres_container, "test_electric")


@pytest.fixture
async def seeded(store: TrajectoryStore) -> Seeded:
    sandbox = f"{SANDBOX}-{uuid4().hex[:8]}"
    source = ReplicationSource()
    started: dict[str, int] = {}
    for index in range(_ITEMS):
        item_id = f"item-{index}"
        started[item_id] = source.append(
            event_pb2.Event(
                item_started=event_pb2.ItemStarted(item_id=item_id, kind=event_pb2.ITEM_KIND_ASSISTANT_TEXT)
            )
        ).cursor
        for part in ("first ", f"body {index}"):
            source.append(event_pb2.Event(text_delta=event_pb2.TextDelta(item_id=item_id, text=part)))
    thread = await store.thread(sandbox, SESSION, source.attached.spec)
    lease = await store.acquire_ingestion(sandbox, timedelta(minutes=2))
    assert lease is not None
    await store.set_attached(thread, source.attached, lease=lease)
    await store.record(thread, source.entries, lease=lease)

    interest = await store.entity_interest(thread)
    assert interest is not None
    # Two deltas, so the generation the chain opened and the revision it has reached are different
    # cursors and a route cannot pass one where it means the other. Resolving them here makes the
    # projector's rule a checked fact: if it changes, this fixture fails instead of a case below.
    owner_id = f"item-{_ITEMS - 1}"
    owner_cursor = started[owner_id]
    selection = await store.payload_selection(
        thread,
        owner_cursor=owner_cursor,
        owner_id=owner_id,
        field=PayloadField.TEXT,
        generation=owner_cursor + 1,
        revision_cursor=owner_cursor + 2,
    )
    assert selection is not None
    assert selection.chunk_count > 0
    return Seeded(thread, interest, selection)


def make_app(upstream: httpx.MockTransport, store: TrajectoryStore) -> tuple[FastAPI, httpx.AsyncClient]:
    electric = httpx.AsyncClient(transport=upstream, base_url="http://electric")
    app = FastAPI()
    app.state.electric = ElectricProxy(electric, store)
    app.include_router(router)
    return app, electric


def entity_query(seeded: Seeded) -> dict[str, str]:
    return {
        "projection_epoch": seeded.interest.scope.projection_epoch,
        "anchor_cursor": str(seeded.interest.anchor_cursor),
        "tail_from": str(seeded.interest.tail_from),
    }


def payload_query(seeded: Seeded) -> dict[str, str]:
    selection = seeded.selection
    return {
        "projection_epoch": selection.scope.projection_epoch,
        "owner_cursor": str(selection.owner_cursor),
        "owner_id": selection.owner_id,
        "field": selection.field,
        "generation": str(selection.generation),
        "revision_cursor": str(selection.revision_cursor),
    }


async def test_entity_shape_names_only_kinds_the_projection_writes(store: TrajectoryStore, seeded: Seeded) -> None:
    """A kind in the predicate that nothing writes is dead, and rides in every shape's cache key."""
    seen: httpx.Request | None = None

    async def upstream(request: httpx.Request) -> httpx.Response:
        nonlocal seen
        seen = request
        return httpx.Response(200, stream=httpx.ByteStream(b"[]"))

    app, electric = make_app(httpx.MockTransport(upstream), store)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://app") as client:
        response = await client.get(
            f"/threads/{seeded.thread}/sync/entities", params=entity_query(seeded) | {"offset": "-1"}
        )
    await electric.aclose()

    assert response.status_code == 200
    assert seen is not None
    named = set(re.findall(r"'([a-z_]+)'", httpx.QueryParams(seen.url.query)["where"]))
    written = set(EntityKind)
    assert named
    assert named <= written, f"predicate names kinds the projection never writes: {sorted(named - written)}"


async def test_entity_shape_is_bounded_and_fixed_by_server(store: TrajectoryStore, seeded: Seeded) -> None:
    seen: httpx.Request | None = None

    async def upstream(request: httpx.Request) -> httpx.Response:
        nonlocal seen
        seen = request
        return httpx.Response(
            200,
            stream=httpx.ByteStream(b'[{"headers":{"control":"up-to-date"}}]'),
            headers={"electric-offset": "7_0", "electric-up-to-date": "true", "x-private": "no"},
        )

    app, electric = make_app(httpx.MockTransport(upstream), store)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://app") as client:
        response = await client.get(
            f"/threads/{seeded.thread}/sync/entities",
            params=entity_query(seeded) | {"offset": "now", "live": "false", "cursor": "cache", "log": "changes_only"},
        )
    await electric.aclose()

    assert response.status_code == 200
    assert response.headers["electric-offset"] == "7_0"
    assert response.headers["electric-up-to-date"] == "true"
    assert response.headers["cache-control"] == "private, no-cache"
    assert "x-private" not in response.headers
    assert seen is not None
    query = httpx.QueryParams(seen.url.query)
    assert query["table"] == "thread_entity"
    assert query["log"] == "changes_only"
    assert query["queryable_columns"] == query["columns"]
    assert query["replica"] == "full"
    assert "cursor >= $3" in query["where"]
    # The shape's lower bound is the interest's, verbatim: the proxy owns the predicate and a
    # browser cannot widen it, and the value is whatever the real resolver computed.
    assert {str(index): query[f"params[{index}]"] for index in range(1, 4)} == {
        "1": str(seeded.thread),
        "2": seeded.interest.scope.projection_epoch,
        "3": str(seeded.interest.tail_from),
    }


async def test_a_re_read_revalidates_and_relays_electric_s_not_modified(store: TrajectoryStore, seeded: Seeded) -> None:
    """Immutable history is cached and revalidated, never re-transferred and never served unchecked."""
    seen: list[httpx.Request] = []

    async def upstream(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.headers.get("if-none-match") == '"shape-7_0"':
            return httpx.Response(304, stream=httpx.ByteStream(b""), headers={"etag": '"shape-7_0"'})
        return httpx.Response(200, stream=httpx.ByteStream(b"[]"), headers={"etag": '"shape-7_0"'})

    app, electric = make_app(httpx.MockTransport(upstream), store)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://app") as client:
        path = f"/threads/{seeded.thread}/sync/entities"
        first = await client.get(path, params=entity_query(seeded) | {"offset": "-1"})
        again = await client.get(
            path, params=entity_query(seeded) | {"offset": "-1"}, headers={"if-none-match": first.headers["etag"]}
        )
    await electric.aclose()

    assert first.status_code == 200
    assert first.headers["etag"] == '"shape-7_0"'
    # Stored, but only ever served through a request this proxy authorized: `no-store` would make
    # the entity tag inert, and a `max-age` would let a cached body outlive the caller's session.
    assert first.headers["cache-control"] == "private, no-cache"
    assert again.status_code == 304
    assert again.content == b""
    assert [request.headers.get("if-none-match") for request in seen] == [None, '"shape-7_0"']


async def test_stale_or_client_widened_interest_is_rejected(store: TrajectoryStore, seeded: Seeded) -> None:
    async def unexpected(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("rejected requests must not reach Electric")

    app, electric = make_app(httpx.MockTransport(unexpected), store)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://app") as client:
        path = f"/threads/{seeded.thread}/sync/entities"
        stale_tail = await client.get(path, params=entity_query(seeded) | {"tail_from": "0", "offset": "-1"})
        arbitrary = await client.get(path, params=entity_query(seeded) | {"table": "event"})
        bad_log = await client.get(path, params=entity_query(seeded) | {"log": "full"})
    await electric.aclose()

    assert stale_tail.status_code == 410
    assert arbitrary.status_code == 400
    assert bad_log.status_code == 400


async def test_old_entity_scope_is_rejected_before_forwarding(store: TrajectoryStore, seeded: Seeded) -> None:
    async def unexpected(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("retired scopes must not reach Electric")

    app, electric = make_app(httpx.MockTransport(unexpected), store)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://app") as client:
        response = await client.get(
            f"/threads/{seeded.thread}/sync/entities",
            params=entity_query(seeded) | {"projection_epoch": "retired-scope"},
        )
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
async def test_current_snapshot_cannot_change_fixed_shape(
    subset: dict[str, str], store: TrajectoryStore, seeded: Seeded
) -> None:
    seen: list[httpx.Request] = []

    async def upstream(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, stream=httpx.ByteStream(b"[]"))

    app, electric = make_app(httpx.MockTransport(upstream), store)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://app") as client:
        response = await client.get(
            f"/threads/{seeded.thread}/sync/entities",
            params=entity_query(seeded) | {"offset": "0_0", "handle": "fixed", **subset},
            headers={"electric-protocol-version": "1.0"},
        )
    await electric.aclose()
    assert response.status_code == 200
    forwarded = seen[0].url.params
    assert forwarded["log"] == "changes_only"
    assert "cursor >= $3" in forwarded["where"]
    assert forwarded["params[3]"] == str(seeded.interest.tail_from)
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
async def test_snapshot_rejects_caller_selection_and_duplicate_parameters(
    query: str, store: TrajectoryStore, seeded: Seeded
) -> None:
    async def unexpected(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("rejected snapshots must not reach Electric")

    app, electric = make_app(httpx.MockTransport(unexpected), store)
    fixed = httpx.QueryParams(entity_query(seeded))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://app") as client:
        response = await client.get(f"/threads/{seeded.thread}/sync/entities?{fixed}&{query}")
    await electric.aclose()
    assert response.status_code == 400


async def test_payload_shape_uses_server_verified_exact_revision(store: TrajectoryStore, seeded: Seeded) -> None:
    seen: httpx.Request | None = None

    async def upstream(request: httpx.Request) -> httpx.Response:
        nonlocal seen
        seen = request
        return httpx.Response(200, stream=httpx.ByteStream(b"[]"))

    app, electric = make_app(httpx.MockTransport(upstream), store)
    selection = seeded.selection
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://app") as client:
        interest = await client.get(f"/threads/{seeded.thread}/sync/payload-interest", params=payload_query(seeded))
        chunks = await client.get(
            f"/threads/{seeded.thread}/sync/payload-chunks", params=payload_query(seeded) | {"offset": "-1"}
        )
        missing = await client.get(
            f"/threads/{seeded.thread}/sync/payload-interest",
            params=payload_query(seeded) | {"revision_cursor": str(selection.revision_cursor + 1)},
        )
    await electric.aclose()

    # The interest is the manifest's, so a reader's extent comes from the row the projector wrote.
    assert interest.json()["chunk_count"] == str(selection.chunk_count)
    assert interest.json()["content_bytes"] == str(selection.content_bytes)
    assert chunks.status_code == 200
    assert missing.status_code == 410
    assert seen is not None
    forwarded = httpx.QueryParams(seen.url.query)
    assert forwarded["table"] == "thread_payload_chunk"
    # The shape stops at the revision's own extent: a later append to the same generation is a
    # different shape, and this one never grows past what its metadata named.
    assert "chunk_index < $7" in forwarded["where"]
    assert forwarded["params[6]"] == str(selection.generation)
    assert forwarded["params[7]"] == str(selection.chunk_count)


async def test_command_shape_is_scoped_bounded_and_includes_settled_or_future_ids(
    store: TrajectoryStore, seeded: Seeded
) -> None:
    seen: list[httpx.Request] = []

    async def upstream(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, stream=httpx.ByteStream(b"[]"))

    app, electric = make_app(httpx.MockTransport(upstream), store)
    scope = seeded.interest.scope
    params = [("projection_epoch", scope.projection_epoch), ("offset", "-1")]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://app") as client:
        response = await client.get(
            f"/threads/{seeded.thread}/sync/commands",
            params=[*params, ("command_id", "future"), ("command_id", "failed"), ("command_id", "failed")],
        )
        assert response.status_code == 200
        for invalid in [
            params,
            [*params, ("command_id", "")],
            [*params, *[("command_id", str(n)) for n in range(129)]],
        ]:
            invalid_response = await client.get(f"/threads/{seeded.thread}/sync/commands", params=tuple(invalid))
            assert invalid_response.status_code == 422
        stale = [(key, "old" if key == "projection_epoch" else value) for key, value in params]
        assert (
            await client.get(f"/threads/{seeded.thread}/sync/commands", params=[*stale, ("command_id", "failed")])
        ).status_code == 410
        assert (
            await client.get(f"/threads/{UUID(int=0)}/sync/commands", params=[*params, ("command_id", "failed")])
        ).status_code == 410
    await electric.aclose()
    assert len(seen) == 1
    query = httpx.QueryParams(seen[0].url.query)
    assert query["where"] == (
        "thread_id = $1 AND projection_epoch = $2 AND entity_kind = 'command' AND entity_id IN ($3,$4)"
    )
    assert query["params[1]"] == str(seeded.thread)
    assert query["params[2]"] == scope.projection_epoch
    assert query["params[3]"] == "failed"
    assert query["params[4]"] == "future"
    assert "command_id" not in query


@pytest.mark.parametrize("disconnect", ["cancel", "send", "receive"])
async def test_slow_downstream_bounds_upstream_reads_and_disconnect_closes_response(
    disconnect: str, store: TrajectoryStore, seeded: Seeded
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

    app, electric = make_app(httpx.MockTransport(upstream), store)
    scope = {
        "type": "http",
        "asgi": {"spec_version": "2.3" if disconnect == "receive" else "2.4"},
        "query_string": b"offset=-1",
        "headers": [],
    }
    response = await app.state.electric.entities(
        Request(scope),
        thread_id=seeded.thread,
        projection_epoch=seeded.interest.scope.projection_epoch,
        anchor_cursor=seeded.interest.anchor_cursor,
        tail_from=seeded.interest.tail_from,
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
