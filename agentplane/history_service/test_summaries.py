"""Session summary pages over a migrated history database, folded by the shadow."""

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
import pytest_bazel
from google.protobuf.timestamp_pb2 import Timestamp
from sqlalchemy.ext.asyncio import AsyncEngine

from agentplane.history_service import protocol_pb2
from agentplane.history_service.fold_shadow import FOLD_EPOCH, FoldShadow
from agentplane.history_service.summaries import SummaryReader
from agentplane.proto import event_log_pb2, event_pb2
from agentplane.sandbox_service.session_history.store import Store

# gazelle:include_dep //agentplane/sandbox_service/session_history:conftest

pytest_plugins = ("agentplane.sandbox_service.session_history.conftest",)

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


async def every_page(
    summaries: SummaryReader, request: protocol_pb2.ListSessionSummariesRequest
) -> list[list[protocol_pb2.SessionSummary]]:
    pages: list[list[protocol_pb2.SessionSummary]] = []
    while True:
        page = await summaries.list_page(request)
        pages.append(list(page.summaries))
        if not page.next_page_token:
            return pages
        request.page_token = page.next_page_token


async def test_pages_newest_first_with_unfolded_sessions_ahead(
    engine: AsyncEngine, history: Store, summaries: SummaryReader
) -> None:
    oldest = await session_in(history, "test-sandbox", started(0))
    newest = await session_in(history, "test-sandbox", started(20))
    middle = await session_in(history, "test-sandbox", started(10))
    empty = await session_in(history, "test-sandbox", [])
    await FoldShadow(engine=engine, history=history, concurrency=2).cycle()
    # Committed after the fold ran, so it lags.
    lagging = await session_in(history, "test-sandbox", started(30))

    pages = await every_page(summaries, protocol_pb2.ListSessionSummariesRequest(limit=2))

    unfolded = sorted([empty, lagging], reverse=True)
    assert [[UUID(summary.session_id) for summary in page] for page in pages] == [unfolded, [newest, middle], [oldest]]
    by_id = {UUID(summary.session_id): summary for page in pages for summary in page}
    assert (by_id[newest].projection_epoch, by_id[newest].folded_through, by_id[newest].committed_cursor) == (
        FOLD_EPOCH,
        1,
        1,
    )
    assert by_id[newest].first_event_at.ToDatetime(tzinfo=UTC) == START + timedelta(minutes=20)
    assert (by_id[lagging].HasField("projection_epoch"), by_id[lagging].committed_cursor) == (False, 1)


async def test_a_sandbox_filter_and_its_token_go_together(history: Store, summaries: SummaryReader) -> None:
    kept = [await session_in(history, "test-kept-sandbox", started(minutes)) for minutes in (0, 1)]
    await session_in(history, "test-other-sandbox", started(2))
    filtered = protocol_pb2.ListSessionSummariesRequest(
        sandbox_namespace="test-namespace", sandbox_name="test-kept-sandbox", limit=1
    )

    pages = await every_page(summaries, filtered)

    assert {UUID(summary.session_id) for page in pages for summary in page} == set(kept)
    first = await summaries.list_page(
        protocol_pb2.ListSessionSummariesRequest(
            sandbox_namespace="test-namespace", sandbox_name="test-kept-sandbox", limit=1
        )
    )
    with pytest.raises(ValueError, match="another filter"):
        await summaries.list_page(protocol_pb2.ListSessionSummariesRequest(limit=1, page_token=first.next_page_token))


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
