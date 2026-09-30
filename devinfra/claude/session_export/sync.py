"""Keep the Postgres mirror level with the account's sessions (design: docs/sync.md)."""

import asyncio
import logging
from dataclasses import dataclass

from devinfra.claude.session_export.api import SessionsApi
from devinfra.claude.session_export.models import SessionSummary, canonical_id, parse_timestamp
from devinfra.claude.session_export.progress import Progress
from devinfra.claude.session_export.store import SessionStore

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CycleResult:
    sessions: int
    behind: int
    events_read: int


async def sync_session(api: SessionsApi, store: SessionStore, session: SessionSummary) -> int:
    """Store the session's events past what the store has; returns how many events were read."""
    session_id = canonical_id(session.id)
    # The value listed before the pass, so events arriving during it leave the session behind for the next one.
    listed_last_event_at = parse_timestamp(session.last_event_at)
    after = await store.resume_after(session_id)
    progress = Progress(session_id, await api.newest_sequence_num(session_id))
    position = after
    async for page in api.iter_event_pages(session_id, after=after):
        for event in page:
            position += 1
            if event.seq != position:
                raise ValueError(f"{session_id=}: expected sequence_num {position}, got {event.sequence_num}")
        await store.append_events(session_id, page)
        progress.report(position)
    await store.mark_synced(session_id, listed_last_event_at)
    return position - after


async def sync_once(api: SessionsApi, store: SessionStore, *, workers: int) -> CycleResult:
    """One cycle: refresh every session's metadata, then read the events of the sessions that have moved on.

    The whole list is read each time rather than stopping at the first unchanged session: a cycle that died
    partway leaves older sessions behind a newer one that is level.
    """
    sessions = [s async for s in api.list_sessions()]
    await store.upsert_sessions(sessions)
    synced = await store.synced_last_event_at()
    behind = [s for s in sessions if synced[canonical_id(s.id)] != parse_timestamp(s.last_event_at)]
    logger.info("listed %d sessions, %d behind", len(sessions), len(behind))
    slots = asyncio.Semaphore(workers)
    events = 0

    async def sync_one(session: SessionSummary) -> None:
        nonlocal events
        async with slots:
            events += await sync_session(api, store, session)

    async with asyncio.TaskGroup() as tasks:  # the first failure cancels the rest; the next cycle resumes
        for session in behind:
            tasks.create_task(sync_one(session))
    logger.info("synced %d sessions, read %d events", len(behind), events)
    return CycleResult(sessions=len(sessions), behind=len(behind), events_read=events)
