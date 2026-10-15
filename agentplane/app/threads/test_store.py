"""Operator metadata over Thread views read from the History Service's fold."""

from datetime import UTC
from uuid import UUID, uuid4

import pytest
import pytest_bazel
from sqlalchemy import delete, update

from agentplane.app.testing.history import SeededEventLogStore
from agentplane.app.testing.thread_test_support import SPEC, event_entry
from agentplane.app.threads.events.event_log import ThreadNotFoundError
from agentplane.app.threads.store import HistorySandboxMissingError, InvalidPageTokenError, ThreadStore
from agentplane.history_service.storage.db import SessionHistory
from agentplane.history_service.testing.history import History
from agentplane.proto import event_pb2, runner_pb2
from agentplane.runner.harness import Harness
from agentplane.runner_gateway.commands.db import Session as GatewaySession

# gazelle:include_dep @pypi//protobuf


async def seed_thread(
    event_logs: SeededEventLogStore, history: History, *, sandbox: str, session: str, cursor: int, **last: object
) -> UUID:
    """A running Thread folded through `cursor`, whose last entry carries `last`."""
    thread = await event_logs.seed(sandbox, session)
    entries = [event_entry(index, harness_started=event_pb2.HarnessStarted()) for index in range(1, cursor)]
    entries.append(event_entry(cursor, **(last or {"harness_started": event_pb2.HarnessStarted()})))
    await history.record(thread, entries)
    await history.set_attached(
        thread,
        runner_pb2.Attached(
            spec=SPEC, last_cursor=cursor, harness_state=runner_pb2.HARNESS_STATE_RUNNING, active_turn_id="turn"
        ),
    )
    return thread


async def stop_harness(history: History, thread: UUID, *, cursor: int) -> None:
    await history.record(thread, [event_entry(cursor, harness_exited=event_pb2.HarnessExited())])
    await history.set_attached(
        thread, runner_pb2.Attached(spec=SPEC, last_cursor=cursor, harness_state=runner_pb2.HARNESS_STATE_STOPPED)
    )


async def test_thread_views_and_operator_edits_need_no_raw_events(
    store: ThreadStore, event_logs: SeededEventLogStore, history: History
) -> None:
    thread = await seed_thread(event_logs, history, sandbox="one", session="test-session-one", cursor=7)
    other = await seed_thread(event_logs, history, sandbox="two", session="test-session-two", cursor=3)
    view = await store.get_thread(thread)
    assert view is not None
    assert view.last_cursor == 7
    assert view.harness_state == "HARNESS_STATE_RUNNING"
    assert view.active_turn_id == "turn"
    assert view.feed_status == "active"
    assert [v.id for v in await store.list_threads(sandbox="one")] == [thread]
    renamed = await store.rename(thread, "retained")
    assert renamed.name == "retained"
    assert renamed.last_cursor == 7
    assert (await store.rename(thread, None)).name is None
    assert (await store.archive(thread)).archived
    assert [v.id for v in await store.list_threads()] == [other]
    assert {v.id for v in await store.list_threads(include_archived=True)} == {thread, other}
    assert not (await store.unarchive(thread)).archived


async def test_thread_identity_comes_from_history_without_gateway_registration(
    store: ThreadStore, event_logs: SeededEventLogStore, history: History
) -> None:
    thread = await seed_thread(event_logs, history, sandbox="history-owned", session="history-only", cursor=2)
    async with history.sessions.begin() as session:
        await session.execute(delete(GatewaySession).where(GatewaySession.id == thread))

    view = await store.get_thread(thread)
    assert view is not None
    assert view.sandbox == "history-owned"
    assert [item.id for item in await store.list_threads(sandbox="history-owned")] == [thread]
    assert [item.id for item in (await store.list_page(limit=20, search="HISTORY-OWNED")).threads] == [thread]


async def test_pending_history_identity_is_hidden_without_losing_pagination_or_editing_metadata(
    store: ThreadStore, event_logs: SeededEventLogStore, history: History
) -> None:
    visible = await seed_thread(event_logs, history, sandbox="visible", session="visible", cursor=2)
    pending = uuid4()
    await event_logs.open(str(pending))
    assert await store.get_thread(pending) is None
    with pytest.raises(ThreadNotFoundError):
        await store.rename(pending, "premature")
    with pytest.raises(ThreadNotFoundError):
        await store.archive(pending)

    found: list[UUID] = []
    token: str | None = None
    for _ in range(3):
        page = await store.list_page(limit=1, page_token=token)
        found.extend(view.id for view in page.threads)
        token = page.next_page_token
        if token is None:
            break
    else:
        pytest.fail("pagination did not advance past the pending History identity")
    assert found == [visible]


async def test_committed_history_without_sandbox_identity_fails_explicitly(
    store: ThreadStore, event_logs: SeededEventLogStore, history: History
) -> None:
    thread = await seed_thread(event_logs, history, sandbox="known", session="missing-identity", cursor=2)
    async with history.sessions.begin() as session:
        await session.execute(
            update(SessionHistory).where(SessionHistory.id == thread).values(sandbox_namespace=None, sandbox_name=None)
        )
    with pytest.raises(HistorySandboxMissingError):
        await store.get_thread(thread)


@pytest.mark.parametrize(
    "status", [None, event_pb2.TURN_STATUS_COMPLETED, event_pb2.TURN_STATUS_FAILED, event_pb2.TURN_STATUS_INTERRUPTED]
)
async def test_thread_turn_status_comes_from_the_fold_summary(
    store: ThreadStore, event_logs: SeededEventLogStore, history: History, status: event_pb2.TurnStatus | None
) -> None:
    last = {} if status is None else {"turn_completed": event_pb2.TurnCompleted(turn_id="turn", status=status)}
    thread = await seed_thread(event_logs, history, sandbox="one", session="test-session", cursor=7, **last)
    await stop_harness(history, thread, cursor=8)
    view = await store.get_thread(thread)
    assert view is not None
    assert view.last_turn_status == (None if status is None else event_pb2.TurnStatus.Name(status))
    assert view.feed_status == "ended"
    assert await store.list_threads() == [view]


async def test_thread_idle_since_comes_from_the_fold_summary(
    store: ThreadStore, event_logs: SeededEventLogStore, history: History
) -> None:
    done = event_pb2.TurnCompleted(turn_id="turn", status=event_pb2.TURN_STATUS_COMPLETED)
    thread = await seed_thread(
        event_logs, history, sandbox="one", session="test-idle-session", cursor=7, turn_completed=done
    )
    view = await store.get_thread(thread)
    assert view is not None
    assert view.idle_since == event_entry(7).event.at.ToDatetime(tzinfo=UTC)


async def test_a_feed_ends_where_its_harness_stops_and_a_resumed_harness_reopens_it(
    store: ThreadStore, event_logs: SeededEventLogStore, history: History
) -> None:
    thread = await seed_thread(event_logs, history, sandbox="one", session="test-resumed-session", cursor=3)
    await stop_harness(history, thread, cursor=4)
    ended = await store.get_thread(thread)
    assert ended is not None
    assert ended.feed_status == "ended"
    await history.record(thread, [event_entry(5, harness_started=event_pb2.HarnessStarted(resumed=True))])
    resumed = await store.get_thread(thread)
    assert resumed is not None
    assert resumed.feed_status == "active"


async def test_harness_model_and_cwd_come_from_the_attached_runner_spec(
    store: ThreadStore, event_logs: SeededEventLogStore, history: History
) -> None:
    attached = await seed_thread(event_logs, history, sandbox="one", session="test-attached-session", cursor=1)
    unattached = await event_logs.seed("one", "test-unattached-session")
    views = {view.id: view for view in await store.list_threads()}
    assert (views[attached].harness, views[attached].model, views[attached].cwd) == (
        Harness(runner_pb2.Harness.Name(SPEC.harness)),
        SPEC.model,
        SPEC.cwd,
    )
    assert (views[unattached].harness, views[unattached].model, views[unattached].cwd) == (None, None, None)


async def test_a_session_not_folded_yet_reads_as_nothing_folded(
    store: ThreadStore, event_logs: SeededEventLogStore
) -> None:
    thread = await event_logs.seed("sb-1", "test-unfolded-session")
    view = await store.get_thread(thread)
    assert view is not None
    assert (view.last_cursor, view.feed_status, view.model) == (0, None, None)
    assert await store.list_threads() == [view]


async def all_pages(
    store: ThreadStore, *, search: str | None = None, include_archived: bool = False
) -> list[list[UUID]]:
    pages: list[list[UUID]] = []
    page_token = None
    while True:
        page = await store.list_page(limit=2, page_token=page_token, search=search, include_archived=include_archived)
        pages.append([view.id for view in page.threads])
        if page.next_page_token is None:
            return pages
        page_token = page.next_page_token


async def test_pages_follow_creation_order_under_each_filter(
    store: ThreadStore, event_logs: SeededEventLogStore, history: History
) -> None:
    oldest, middle, newest = [
        await seed_thread(event_logs, history, sandbox=f"test-sandbox-{n}", session=f"test-session-{n}", cursor=1)
        for n in range(3)
    ]
    newest_other = await seed_thread(event_logs, history, sandbox="test-other", session="test-other-session", cursor=1)
    await store.rename(middle, "Searched thread")
    await store.archive(newest)

    assert await all_pages(store) == [[newest_other, middle], [oldest]]
    assert await all_pages(store, include_archived=True) == [[newest_other, newest], [middle, oldest]]
    assert await all_pages(store, search="SEARCHED") == [[middle]]
    assert await all_pages(store, search="test-sandbox", include_archived=True) == [[newest, middle], [oldest]]
    assert await all_pages(store, search=str(oldest)[:8]) == [[oldest]]
    assert await all_pages(store, search="%") == [[]]
    assert await store.archived_count() == 1


async def test_a_page_token_continues_only_its_own_listing(store: ThreadStore, event_logs: SeededEventLogStore) -> None:
    for n in range(3):
        await event_logs.seed(f"test-sandbox-{n}", f"test-session-{n}")
    first = await store.list_page(limit=1)
    assert first.next_page_token is not None

    with pytest.raises(InvalidPageTokenError, match="another listing"):
        await store.list_page(limit=1, page_token=first.next_page_token, include_archived=True)
    with pytest.raises(InvalidPageTokenError, match="malformed"):
        await store.list_page(limit=1, page_token="test-not-a-token")


if __name__ == "__main__":
    pytest_bazel.main()
