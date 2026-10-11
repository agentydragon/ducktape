"""Entity evidence and native frames over a migrated history database, folded by the shadow."""

from uuid import UUID, uuid4

import pytest
import pytest_bazel
from sqlalchemy.ext.asyncio import AsyncEngine

from agentplane.history_service import protocol_pb2
from agentplane.history_service.evidence import EvidenceNotFoundError, EvidenceReader
from agentplane.history_service.fold_shadow import FOLD_EPOCH, FoldShadow
from agentplane.history_service.storage.store import Store
from agentplane.history_service.testing.sessions import WRITER
from agentplane.history_service.thread_reads import ThreadScopeResetError
from agentplane.proto import event_log_pb2, event_pb2
from agentplane.sandbox_service.testing.delta_spans import claude_text
from agentplane.thread_fold.views import EntityKind

# gazelle:include_dep @pypi//protobuf
# gazelle:include_dep //agentplane/history_service/storage:conftest

pytest_plugins = ("agentplane.history_service.storage.conftest",)

REPLY = protocol_pb2.EntityKey(entity_kind=EntityKind.ITEM, entity_id="test-reply")
NATIVE = event_pb2.Native(direction=event_pb2.DIRECTION_FROM_HARNESS, line='{"test": "native frame"}')


def entry(cursor: int, *, natives: tuple[int, ...] = (), **observation: object) -> event_log_pb2.EventEntry:
    event = event_pb2.Event(**observation)  # type: ignore[arg-type]
    event.source_sequences.extend(natives)
    return event_log_pb2.EventEntry(
        cursor=cursor, origin=event_log_pb2.EventOrigin(source_id="test-runner", sequence=cursor), event=event
    )


@pytest.fixture
def history(engine: AsyncEngine) -> Store:
    return Store(engine)


@pytest.fixture
def reader(engine: AsyncEngine, history: Store) -> EvidenceReader:
    return EvidenceReader(engine, history)


@pytest.fixture
async def folded(engine: AsyncEngine, history: Store) -> UUID:
    session_id = uuid4()
    await history.bind_writer(session_id, WRITER)
    await history.append(
        session_id,
        [
            entry(1, native=NATIVE),
            entry(2, turn_started=event_pb2.TurnStarted(turn_id="test-turn")),
            entry(
                3,
                natives=(1,),
                item_started=event_pb2.ItemStarted(item_id="test-reply", kind=event_pb2.ITEM_KIND_ASSISTANT_TEXT),
            ),
            entry(4, text_delta=event_pb2.TextDelta(item_id="test-reply", text="hello")),
        ],
    )
    await FoldShadow(engine=engine, history=history, concurrency=1, page_size=1000).cycle()
    return session_id


async def test_evidence_pages_lead_to_the_native_frame(reader: EvidenceReader, folded: UUID) -> None:
    observations: list[protocol_pb2.EvidenceObservation] = []
    request = protocol_pb2.ReadEntityEvidenceRequest(
        session_id=str(folded), projection_epoch=FOLD_EPOCH, entity=REPLY, limit=1
    )
    while True:
        page = await reader.evidence(request)
        observations.extend(page.observations)
        if not page.HasField("next_after_cursor"):
            break
        request.after_cursor = page.next_after_cursor
    assert protocol_pb2.EvidenceObservation(observation_cursor=3, has_native=True) in observations
    assert protocol_pb2.EvidenceObservation(observation_cursor=4, has_native=False) in observations

    frames = await reader.native_frames(
        protocol_pb2.ReadNativeFramesRequest(
            session_id=str(folded), projection_epoch=FOLD_EPOCH, entity=REPLY, observation_cursor=3, limit=10
        )
    )

    assert [(frame.source_sequence, frame.entry.event.native) for frame in frames.frames] == [(1, NATIVE)]
    assert not frames.HasField("next_after_sequence")


async def test_a_frame_settled_before_the_fold_forgets_its_link_is_unavailable(
    engine: AsyncEngine, reader: EvidenceReader
) -> None:
    settling = Store(engine, settle_deltas=True)
    # 1-2 start, 3-6 two chunk pairs, 7-8 completion; the fold links chunk 4 to its frame 3.
    span = claude_text(["Hel", "lo"]).entries
    session_id = uuid4()
    await settling.bind_writer(session_id, WRITER)
    await settling.append(session_id, span[:6])
    await FoldShadow(engine=engine, history=settling, concurrency=1, page_size=1000).cycle()
    await settling.append(session_id, span[6:])

    frames = await reader.native_frames(
        protocol_pb2.ReadNativeFramesRequest(
            session_id=str(session_id),
            projection_epoch=FOLD_EPOCH,
            entity=protocol_pb2.EntityKey(entity_kind=EntityKind.ITEM, entity_id="msg_test#0"),
            observation_cursor=4,
            limit=10,
        )
    )

    assert [(frame.source_sequence, frame.HasField("entry")) for frame in frames.frames] == [(3, False)]


async def test_evidence_refuses_a_stale_epoch_and_an_unlinked_observation(reader: EvidenceReader, folded: UUID) -> None:
    with pytest.raises(ThreadScopeResetError):
        await reader.evidence(
            protocol_pb2.ReadEntityEvidenceRequest(session_id=str(folded), projection_epoch="v1", entity=REPLY, limit=1)
        )
    with pytest.raises(EvidenceNotFoundError):
        await reader.native_frames(
            protocol_pb2.ReadNativeFramesRequest(
                session_id=str(folded), projection_epoch=FOLD_EPOCH, entity=REPLY, observation_cursor=2, limit=1
            )
        )


if __name__ == "__main__":
    pytest_bazel.main()
