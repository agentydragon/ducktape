"""Entity windows and payload chunks over a migrated history database, folded by the shadow."""

from uuid import UUID, uuid4

import pytest
import pytest_bazel
from pydantic import TypeAdapter
from sqlalchemy.ext.asyncio import AsyncEngine

from agentplane.history_service import protocol_pb2
from agentplane.history_service.fold_shadow import FOLD_EPOCH, PAGE_SIZE, FoldShadow
from agentplane.history_service.thread_reads import ThreadReader, ThreadScopeResetError
from agentplane.protocol import event_log_pb2, event_pb2
from agentplane.sandbox_service.session_history.store import HistoryNotFoundError, Store
from agentplane.thread_fold.views import EntityKind, ThreadEntityView, ThreadItemEntityView

# gazelle:include_dep @pypi//protobuf
# gazelle:include_dep //agentplane/sandbox_service/session_history:conftest

pytest_plugins = ("agentplane.sandbox_service.session_history.conftest",)

VIEW: TypeAdapter[ThreadEntityView] = TypeAdapter(ThreadEntityView)
# More deltas than a fold page, so the reply's text spans chunks written by separate batches.
DELTAS = PAGE_SIZE + 2


def entry(cursor: int, **observation: object) -> event_log_pb2.EventEntry:
    return event_log_pb2.EventEntry(
        cursor=cursor,
        origin=event_log_pb2.EventOrigin(source_id="test-runner", sequence=cursor),
        event=event_pb2.Event(**observation),  # type: ignore[arg-type]
    )


@pytest.fixture
def history(engine: AsyncEngine) -> Store:
    return Store(engine)


@pytest.fixture
def reader(engine: AsyncEngine) -> ThreadReader:
    return ThreadReader(engine)


@pytest.fixture
async def folded(engine: AsyncEngine, history: Store) -> UUID:
    session_id = uuid4()
    await history.open(
        session_id,
        sandbox_namespace="test-namespace",
        sandbox_name="test-sandbox",
        sandbox_uid=None,
        runner_session_id="test-runner-session",
    )
    await history.append(
        session_id,
        [
            entry(1, turn_started=event_pb2.TurnStarted(turn_id="test-turn")),
            entry(2, item_started=event_pb2.ItemStarted(item_id="test-reply", kind=event_pb2.ITEM_KIND_ASSISTANT_TEXT)),
            *(
                entry(3 + index, text_delta=event_pb2.TextDelta(item_id="test-reply", text=str(index % 10)))
                for index in range(DELTAS)
            ),
        ],
    )
    shadow = FoldShadow(engine=engine, history=history, concurrency=1)
    while (await shadow.cycle()).behind:
        pass
    return session_id


async def test_a_range_window_and_its_payload_read_back_the_streamed_reply(reader: ThreadReader, folded: UUID) -> None:
    window = await reader.entities(
        protocol_pb2.ReadThreadEntitiesRequest(
            session_id=str(folded),
            projection_epoch=FOLD_EPOCH,
            range=protocol_pb2.EntityRange(start_entity_index=0, limit=200),
        )
    )

    assert (window.projection_epoch, window.through_cursor) == (FOLD_EPOCH, 2 + DELTAS)
    assert [entity.entity_index for entity in window.entities] == list(range(len(window.entities)))
    views = [VIEW.validate_json(entity.view_json) for entity in window.entities]
    reply = next(view for view in views if isinstance(view, ThreadItemEntityView))
    assert reply.text_ref is not None
    ref = protocol_pb2.PayloadRef(
        projection_epoch=reply.text_ref.projection_epoch,
        owner_cursor=int(reply.text_ref.owner_cursor),
        owner_id=reply.text_ref.owner_id,
        field=reply.text_ref.field,
        generation=int(reply.text_ref.generation),
        revision_cursor=int(reply.text_ref.revision_cursor),
    )
    chunks: list[str] = []
    while True:
        page = await reader.payload(
            protocol_pb2.ReadPayloadRequest(session_id=str(folded), ref=ref, start_chunk=len(chunks), limit=1)
        )
        chunks.extend(page.chunks)
        if len(chunks) >= page.chunk_count:
            break
    assert page.chunk_count == int(reply.text_ref.chunk_count) > 1
    assert "".join(chunks) == "".join(str(index % 10) for index in range(DELTAS))


async def test_keys_return_the_rows_that_exist(reader: ThreadReader, folded: UUID) -> None:
    window = await reader.entities(
        protocol_pb2.ReadThreadEntitiesRequest(
            session_id=str(folded),
            keys=protocol_pb2.EntityKeys(
                keys=[
                    protocol_pb2.EntityKey(entity_kind=EntityKind.ITEM, entity_id="test-reply"),
                    protocol_pb2.EntityKey(entity_kind=EntityKind.COMMAND, entity_id="test-never-sent"),
                ]
            ),
        )
    )

    assert [VIEW.validate_json(entity.view_json).entity_id for entity in window.entities] == ["test-reply"]


async def test_reads_refuse_a_stale_epoch_and_an_unknown_session(reader: ThreadReader, folded: UUID) -> None:
    window = protocol_pb2.EntityRange(start_entity_index=0, limit=10)
    with pytest.raises(ThreadScopeResetError):
        await reader.entities(
            protocol_pb2.ReadThreadEntitiesRequest(session_id=str(folded), projection_epoch="v1", range=window)
        )
    with pytest.raises(HistoryNotFoundError):
        await reader.entities(protocol_pb2.ReadThreadEntitiesRequest(session_id=str(uuid4()), range=window))


if __name__ == "__main__":
    pytest_bazel.main()
