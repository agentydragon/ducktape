"""Session summaries over a migrated history database, folded by the shadow."""

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
import pytest_bazel
from google.protobuf.timestamp_pb2 import Timestamp
from sqlalchemy.ext.asyncio import AsyncEngine

from agentplane.history_service.fold_runner import FOLD_EPOCH, FoldRunner
from agentplane.history_service.storage.store import Store
from agentplane.history_service.summaries import SummaryReader
from agentplane.proto import event_log_pb2, event_pb2

# gazelle:include_dep //agentplane/history_service/storage:conftest

pytest_plugins = ("agentplane.history_service.storage.conftest",)

START = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)


def started(minutes: int) -> list[event_log_pb2.EventEntry]:
    at = Timestamp()
    at.FromDatetime(START + timedelta(minutes=minutes))
    return [
        event_log_pb2.EventEntry(
            cursor=1,
            origin=event_log_pb2.EventOrigin(source_id="test-runner", sequence=1),
            event=event_pb2.Event(at=at, harness_started=event_pb2.HarnessStarted(pid=7)),
        )
    ]


@pytest.fixture
def history(engine: AsyncEngine) -> Store:
    return Store(engine)


@pytest.fixture
def summaries(engine: AsyncEngine) -> SummaryReader:
    return SummaryReader(engine)


async def session_in(history: Store, sandbox: str, entries: list[event_log_pb2.EventEntry]) -> UUID:
    session_id = uuid4()
    await history.record_session(
        session_id,
        sandbox_namespace="test-namespace",
        sandbox_name=sandbox,
        sandbox_uid=None,
        runner_session_id=f"test-runner-{session_id}",
    )
    await history.append(session_id, entries)
    return session_id


async def test_a_summary_reports_the_fold_beside_committed_history(
    engine: AsyncEngine, history: Store, summaries: SummaryReader
) -> None:
    folded = await session_in(history, "test-sandbox", started(20))
    await FoldRunner(engine=engine, history=history, concurrency=2, page_size=1000).cycle()
    # Committed after the fold ran, so it lags.
    lagging = await session_in(history, "test-sandbox", started(30))

    by_id = {UUID(summary.session_id): summary for summary in await summaries.get_many({folded, lagging})}

    assert (by_id[folded].projection_epoch, by_id[folded].folded_through, by_id[folded].committed_cursor) == (
        FOLD_EPOCH,
        1,
        1,
    )
    assert by_id[folded].first_event_at.ToDatetime(tzinfo=UTC) == START + timedelta(minutes=20)
    assert (by_id[lagging].HasField("projection_epoch"), by_id[lagging].committed_cursor) == (False, 1)


async def test_a_batch_read_leaves_out_sessions_without_history(history: Store, summaries: SummaryReader) -> None:
    named = {await session_in(history, "test-sandbox", started(minutes)) for minutes in (0, 1)}
    await session_in(history, "test-sandbox", started(2))

    read = await summaries.get_many(named | {uuid4()})

    assert {UUID(summary.session_id) for summary in read} == named


async def test_a_batch_read_is_bounded(summaries: SummaryReader) -> None:
    with pytest.raises(ValueError, match="too many"):
        await summaries.get_many({uuid4() for _ in range(201)})


if __name__ == "__main__":
    pytest_bazel.main()
