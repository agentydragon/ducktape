"""Entity windows over a migrated history database, folded by the History Service fold."""

from uuid import UUID, uuid4

import pytest
import pytest_bazel
from pydantic import TypeAdapter
from sqlalchemy.ext.asyncio import AsyncEngine

from agentplane.history_service import protocol_pb2
from agentplane.history_service.fold.views import EntityKind, ThreadEntityView, ThreadItemEntityView
from agentplane.history_service.fold_runner import FOLD_EPOCH, FoldRunner
from agentplane.history_service.storage.store import HistoryNotFoundError, Store
from agentplane.history_service.testing.sessions import WRITER
from agentplane.history_service.thread_reads import ThreadReader, ThreadScopeResetError
from agentplane.proto import event_log_pb2, event_pb2

# gazelle:include_dep @pypi//protobuf
# gazelle:include_dep //agentplane/history_service/storage:conftest

pytest_plugins = ("agentplane.history_service.storage.conftest",)

VIEW: TypeAdapter[ThreadEntityView] = TypeAdapter(ThreadEntityView)
PAGE_SIZE = 8
# More deltas than a fold page, so the window reflects several fold batches.
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
    await history.bind_writer(session_id, WRITER)
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
    folder = FoldRunner(engine=engine, history=history, concurrency=1, page_size=PAGE_SIZE)
    while (await folder.cycle()).behind:
        pass
    return session_id


async def test_a_range_window_reads_back_the_folded_stream(reader: ThreadReader, folded: UUID) -> None:
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
    assert reply.entity_id == "test-reply"


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
