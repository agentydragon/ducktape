import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import pytest_bazel
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine
from tenacity import AsyncRetrying, retry_if_exception_type, stop_after_delay, wait_fixed

from agentplane.app.threads.events.event_log import EventLogStore
from agentplane.app.threads.history_watch import HistoryWatch, SessionOpenings
from agentplane.app.threads.summaries import Summaries
from agentplane.changes import ChangeSignal
from agentplane.history_service.client import HistoryServiceClient
from agentplane.history_service.testing.backend import announce_fold_commit, history_service
from agentplane.history_service.testing.history import History
from agentplane.history_service.testing.sessions import WRITER


async def test_fold_commits_and_renewals_wake_thread_readers(service_history_db_url: str, tmp_path: Path) -> None:
    changes = ChangeSignal()
    woken = asyncio.Event()
    async with history_service(service_history_db_url, tmp_path / "history-token", watch_lease_s=3) as history:
        watch = asyncio.create_task(HistoryWatch(history, changes.notify, retry_interval_s=0.05).run())
        try:
            with changes.subscribe(woken):
                await asyncio.wait_for(woken.wait(), 10)  # the stream's opening notice
                woken.clear()
                await announce_fold_commit(service_history_db_url)
                await asyncio.wait_for(woken.wait(), 10)
                woken.clear()
                # After planned renewal the next stream opens with a notice of its own.
                await asyncio.wait_for(woken.wait(), 10)
        finally:
            watch.cancel()
            await asyncio.gather(watch, return_exceptions=True)


@pytest.fixture
def production_event_logs(
    engine: AsyncEngine, history_client: HistoryServiceClient, summaries: Summaries
) -> EventLogStore:
    """The deployed store, which confirms each Session with the History Service before opening its Thread."""
    return EventLogStore(engine, history_reader=history_client, summaries=summaries)


@asynccontextmanager
async def session_openings(
    service_history_db_url: str, token_file: Path, event_logs: EventLogStore
) -> AsyncIterator[None]:
    """Session openings on their own History Service client, as `main.py` runs them."""
    async with history_service(service_history_db_url, token_file) as client:
        task = asyncio.create_task(SessionOpenings(client, event_logs, retry_interval_s=0.05).run())
        try:
            yield
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def wait_for_thread(event_logs: EventLogStore, session_id: UUID) -> None:
    async for attempt in AsyncRetrying(
        stop=stop_after_delay(10), wait=wait_fixed(0.05), retry=retry_if_exception_type(AssertionError), reraise=True
    ):
        with attempt:
            assert await event_logs.exists(session_id)


async def test_each_recorded_session_gets_a_thread(
    service_history_db_url: str, tmp_path: Path, history: History, production_event_logs: EventLogStore
) -> None:
    recorded_before = uuid4()
    await history.store.bind_writer(recorded_before, WRITER)
    async with session_openings(service_history_db_url, tmp_path / "openings-token", production_event_logs):
        # Recorded before the replica watched: the stream's backlog.
        await wait_for_thread(production_event_logs, recorded_before)
        recorded_while_watching = uuid4()
        await history.store.bind_writer(recorded_while_watching, WRITER)
        await wait_for_thread(production_event_logs, recorded_while_watching)


async def test_a_session_whose_open_failed_is_offered_again_on_the_next_stream(
    service_history_db_url: str,
    tmp_path: Path,
    history: History,
    production_event_logs: EventLogStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts: list[str] = []
    open_thread = production_event_logs.open

    async def failing_once(session_id: str) -> UUID:
        attempts.append(session_id)
        if len(attempts) == 1:
            raise SQLAlchemyError("test: the app database is unavailable")
        return await open_thread(session_id)

    monkeypatch.setattr(production_event_logs, "open", failing_once)
    session_id = uuid4()
    await history.store.bind_writer(session_id, WRITER)
    async with session_openings(service_history_db_url, tmp_path / "openings-token", production_event_logs):
        await wait_for_thread(production_event_logs, session_id)
    assert attempts[:2] == [str(session_id), str(session_id)]


if __name__ == "__main__":
    pytest_bazel.main()
