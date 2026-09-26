"""A body's chunks compacted in one transaction, as the readers following its field receive it,
pinned against the Electric we deploy.

A library spike for compacting a finished body: its chunks, one per ingestion batch, rewritten as
one row at chunk 0, so that neither the body's generation nor any reference to it moves and a
reader holding the body has nothing to refetch. Readers go through the proxy and its shape; one
comparison shape differs from it only by leaving out `replica=full`. The facts a compaction design
depends on, each under the question it answers:

- Can a reader holding the body apply the compaction without withdrawing text it shows? The whole
  transaction arrives as one batch before a single `up-to-date`, in statement order, its final
  change marked `last`.
- Does a chunk's delete carry its text? Under `replica=full` it carries the whole row the reader
  holds, and a rewrite carries the old text beside the new; without it, a delete carries only the
  row's key.
- Does the compacted row reach every reader of the field? A reader that never loaded the body
  receives exactly what a holder does, the whole text included: the live log is the shape's, not
  the reader's.
- What does a reader loading the body afterwards get? The pair subset that loaded the chunks
  answers with the compacted row alone.

Not pinned: how the compacted row answers a reference naming an intermediate revision. That needs
the lengths of the chunks it replaced, a column the chunk table does not have.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from uuid import UUID

import httpx
import pytest_bazel
from fastapi import FastAPI
from sqlalchemy import ColumnElement, delete, inspect, select, update
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from agentplane.app.agent_runtime.events.event_log import EventLogStore
from agentplane.app.agent_runtime.ingestion import Ingestion
from agentplane.app.agent_runtime.models import ThreadEntity, ThreadPayloadChunk
from agentplane.app.agent_runtime.view.content import ContentStore
from agentplane.app.agent_runtime.view.views import ThreadPayloadReference
from agentplane.app.changes import Changes
from agentplane.app.database import connect
from agentplane.app.electric import ElectricProxy, router
from agentplane.app.testing.electric_service import electric_service
from agentplane.app.testing.replication_source import SANDBOX, SESSION, ReplicationSource
from agentplane.protocol import event_pb2

# gazelle:include_dep @pypi//protobuf

Row = dict[str, Any]
Message = dict[str, Any]
# A body streamed in three ingestion batches, each written as one chunk.
_STREAMED = ("Streamed ", "in three ", "batches.")
_TEXT = "".join(_STREAMED)
_ITEM = "streamed"
_KEY = {column.name for column in inspect(ThreadPayloadChunk).primary_key}


@dataclass(frozen=True)
class _Shape:
    """One shape's requests: through the proxy, or to Electric with explicit parameters."""

    client: httpx.AsyncClient
    path: str
    params: dict[str, str]


@dataclass(frozen=True)
class _Reader:
    """A reader of one shape, from where it opened it."""

    shape: _Shape
    handle: str
    offset: str

    async def load(self, reference: ThreadPayloadReference) -> list[Row]:
        """The body a reference names, read as the browser reads it: one (owner, generation) pair."""
        response = await self.shape.client.post(
            self.shape.path,
            params=self.shape.params | {"handle": self.handle, "offset": self.offset},
            json={
                "where": "(owner_id = $1 AND generation = $2)",
                "params": {"1": reference.owner_id, "2": reference.generation},
            },
        )
        assert response.status_code == 200, response.text
        return sorted(
            (message["value"] for message in response.json()["data"]), key=lambda row: int(row["chunk_index"])
        )

    async def follow(self) -> list[list[Message]]:
        """The live log from where the reader opened, as the batches a client applies — each the
        changes before the next `up-to-date` — until one holds a change."""
        batches: list[list[Message]] = [[]]
        offset = self.offset
        while not any(batches[:-1]):
            response = await self.shape.client.get(
                self.shape.path, params=self.shape.params | {"handle": self.handle, "offset": offset, "live": "true"}
            )
            response.raise_for_status()
            offset = response.headers["electric-offset"]
            for message in response.json():
                match message["headers"].get("control"):
                    case "up-to-date":
                        batches.append([])
                    case None:
                        batches[-1].append(message)
                    case control:
                        raise AssertionError(f"unexpected control message: {control=} {message=}")
        return [batch for batch in batches if batch]


async def _open(shape: _Shape) -> _Reader:
    """Opens the shape from now, as the browser does: no rows, only where its live log starts."""
    response = await shape.client.get(shape.path, params=shape.params | {"offset": "now"})
    response.raise_for_status()
    return _Reader(shape, response.headers["electric-handle"], response.headers["electric-offset"])


@dataclass(frozen=True)
class _FollowedBody:
    """A streamed body, and readers following its field from before it is compacted."""

    engine: AsyncEngine
    thread: UUID
    reference: ThreadPayloadReference
    proxy: _Shape
    # Loaded the body's chunks through the proxy.
    holder: _Reader
    held: list[Row]
    # Opened the proxy's shape and loaded nothing.
    bystander: _Reader
    # The proxy's shape without `replica=full`, straight to Electric.
    keyed: _Reader

    @property
    def chunks(self) -> tuple[ColumnElement[bool], ...]:
        return (
            ThreadPayloadChunk.thread_id == self.thread,
            ThreadPayloadChunk.projection_epoch == self.reference.projection_epoch,
            ThreadPayloadChunk.owner_cursor == int(self.reference.owner_cursor),
            ThreadPayloadChunk.owner_id == self.reference.owner_id,
            ThreadPayloadChunk.field == self.reference.field,
            ThreadPayloadChunk.generation == int(self.reference.generation),
        )

    async def load_afresh(self) -> list[Row]:
        return await (await _open(self.proxy)).load(self.reference)


@asynccontextmanager
async def _followed_body() -> AsyncIterator[_FollowedBody]:
    async with electric_service() as service:
        engine = connect(service.database_url)
        forwarded: list[httpx.Request] = []

        async def forward(request: httpx.Request) -> None:
            forwarded.append(request)

        try:
            event_logs, ingestion = EventLogStore(engine), Ingestion(engine)
            source = ReplicationSource()
            thread = await event_logs.open(SANDBOX, SESSION, source.attached.spec)
            lease = await ingestion.acquire(SANDBOX, timedelta(minutes=2))
            assert lease is not None
            await ingestion.set_attached(thread, source.attached, lease=lease)
            started = (
                event_pb2.Event(harness_started=event_pb2.HarnessStarted(pid=1)),
                event_pb2.Event(turn_started=event_pb2.TurnStarted(turn_id="turn", model="test-model")),
                event_pb2.Event(
                    item_started=event_pb2.ItemStarted(item_id=_ITEM, kind=event_pb2.ITEM_KIND_ASSISTANT_TEXT)
                ),
            )
            for index, text in enumerate(_STREAMED):
                delta = event_pb2.Event(text_delta=event_pb2.TextDelta(item_id=_ITEM, text=text))
                events = (*started, delta) if index == 0 else (delta,)
                await ingestion.record(thread, [source.append(event) for event in events], lease=lease)
            async with async_sessionmaker(engine)() as session:
                text_ref = await session.scalar(
                    select(ThreadEntity.text_ref).where(
                        ThreadEntity.thread_id == thread, ThreadEntity.entity_id == _ITEM
                    )
                )
            reference = ThreadPayloadReference.model_validate(text_ref)
            assert reference.chunk_count == str(len(_STREAMED))
            content = ContentStore(engine)
            scope = await content.current_scope(thread)
            assert scope is not None
            async with (
                asyncio.timeout(120),
                httpx.AsyncClient(base_url=service.url, timeout=35, event_hooks={"request": [forward]}) as electric,
            ):
                app = FastAPI()
                app.state.electric = ElectricProxy(electric, content, event_logs=event_logs, thread_changes=Changes())
                app.include_router(router)
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://app") as browser:
                    proxy = _Shape(
                        browser, f"/threads/{thread}/sync/chunks/text", {"projection_epoch": scope.projection_epoch}
                    )
                    holder = await _open(proxy)
                    [opened] = forwarded
                    held = await holder.load(reference)
                    assert [row["text"] for row in held] == list(_STREAMED)
                    bystander = await _open(proxy)
                    keyed_shape = _Shape(
                        electric,
                        "/v1/shape",
                        {key: value for key, value in opened.url.params.items() if key not in {"offset", "replica"}},
                    )
                    yield _FollowedBody(
                        engine=engine,
                        thread=thread,
                        reference=reference,
                        proxy=proxy,
                        holder=holder,
                        held=held,
                        bystander=bystander,
                        keyed=await _open(keyed_shape),
                    )
        finally:
            await engine.dispose()


def _operations(batches: list[list[Message]]) -> list[list[tuple[str, int]]]:
    return [
        [(change["headers"]["operation"], int(change["value"]["chunk_index"])) for change in batch] for batch in batches
    ]


def _key(row: Row) -> Row:
    return {column: row[column] for column in _KEY}


async def test_compacting_in_place_reaches_every_reader_of_the_field_as_one_batch() -> None:
    """Chunk 0 rewritten to the whole text and the other chunks deleted: every row the compacted body
    keeps is one a holder of the uncompacted body already has."""
    async with _followed_body() as body:
        async with async_sessionmaker(body.engine)() as session, session.begin():
            await session.execute(
                update(ThreadPayloadChunk).where(*body.chunks, ThreadPayloadChunk.chunk_index == 0).values(text=_TEXT)
            )
            await session.execute(delete(ThreadPayloadChunk).where(*body.chunks, ThreadPayloadChunk.chunk_index > 0))
        holder, bystander, keyed = [await reader.follow() for reader in (body.holder, body.bystander, body.keyed)]

        assert _operations(holder) == [[("update", 0), ("delete", 1), ("delete", 2)]]
        [batch] = holder
        # Each change also names its transaction, and the last says so: a client can find the
        # transaction's end without relying on where a response ends.
        assert len({tuple(change["headers"]["txids"]) for change in batch}) == 1
        assert [change["headers"].get("last") for change in batch] == [None, None, True]
        # Under `replica=full` the update carries the whole row and chunk 0's old text, and each
        # delete the whole row it removes.
        [rewritten, *deleted] = batch
        assert rewritten["value"] == body.held[0] | {"text": _TEXT}
        assert rewritten["old_value"] == {"text": _STREAMED[0]}
        assert [change["value"] for change in deleted] == body.held[1:]

        assert bystander == holder

        assert _operations(keyed) == _operations(holder)
        [[keyed_rewritten, *keyed_deleted]] = keyed
        assert keyed_rewritten["value"] == _key(body.held[0]) | {"text": _TEXT}
        assert "old_value" not in keyed_rewritten
        assert [change["value"] for change in keyed_deleted] == [_key(row) for row in body.held[1:]]

        assert await body.load_afresh() == [body.held[0] | {"text": _TEXT}]


async def test_compacting_by_reinsertion_reaches_every_reader_of_the_field_as_one_batch() -> None:
    """Every chunk deleted and the compacted row inserted at chunk 0's key. Electric passes both halves
    on, so a holder keeps its text on screen only by applying the batch whole: applied change by
    change, chunk 0's delete withdraws it."""
    async with _followed_body() as body:
        async with async_sessionmaker(body.engine)() as session, session.begin():
            await session.execute(delete(ThreadPayloadChunk).where(*body.chunks))
            session.add(
                ThreadPayloadChunk(
                    thread_id=body.thread,
                    projection_epoch=body.reference.projection_epoch,
                    owner_cursor=int(body.reference.owner_cursor),
                    owner_id=body.reference.owner_id,
                    field=body.reference.field,
                    generation=int(body.reference.generation),
                    chunk_index=0,
                    text=_TEXT,
                )
            )
        holder, bystander = [await reader.follow() for reader in (body.holder, body.bystander)]

        assert _operations(holder) == [[("delete", 0), ("delete", 1), ("delete", 2), ("insert", 0)]]
        [[*deleted, inserted]] = holder
        assert [change["value"] for change in deleted] == body.held
        assert inserted["value"] == body.held[0] | {"text": _TEXT}

        assert bystander == holder

        assert await body.load_afresh() == [body.held[0] | {"text": _TEXT}]


if __name__ == "__main__":
    pytest_bazel.main()
