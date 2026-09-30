"""Follow the sessions that are moving through the server's event streams (design: docs/sync.md § Live following).

A polling cycle reads what is behind every few minutes. Between cycles this keeps an event stream open per recently
active session, so a running session reaches the mirror as it happens. New sessions are found two ways: the session
watch pushes each change, and a list of the newest sessions every `DISCOVERY_INTERVAL` catches whatever the watch
missed. The streams only add to what a cycle would read, and every
stream event goes through the same contiguity rule: an event past a gap is never stored, the session is paged instead.
"""

import asyncio
import contextlib
import logging
from datetime import UTC, datetime, timedelta

from pydantic import BaseModel, Field
from tenacity import AsyncRetrying, RetryCallState, retry_if_exception, wait_exponential_jitter
from tenacity.wait import wait_base

from devinfra.claude.session_export.api import ResumePointLostError, SessionsApi
from devinfra.claude.session_export.failures import describe_failure
from devinfra.claude.session_export.models import SessionSummary
from devinfra.claude.session_export.store import SessionStore
from devinfra.claude.session_export.sync import read_events_after

logger = logging.getLogger(__name__)

# How often the newest sessions are listed and the followed set re-derived when nothing prompts it. This is also what
# retires a session whose last event has aged out of the window.
DISCOVERY_INTERVAL = 30.0
DISCOVERY = "session discovery"
WATCH = "session watch"
_RETRY_MAX_WAIT = 300.0


class LiveProblem(BaseModel):
    source: str = Field(description="What is failing: `session discovery`, or the id of a session whose stream is.")
    at: datetime = Field(description="When it last failed; it retries with growing waits until it works.")
    message: str


def _is_retryable(failure: BaseException) -> bool:
    """Every ordinary failure but a lost resume point. Not cancellation: tenacity's `with attempt:` catches every
    exception, so a predicate that matched `CancelledError` would retry it and the task could never be stopped."""
    return isinstance(failure, Exception) and not isinstance(failure, ResumePointLostError)


class LiveFollower:
    def __init__(
        self,
        api: SessionsApi,
        store: SessionStore,
        *,
        max_streams: int,
        window: timedelta,
        retry_wait: wait_base | None = None,
    ) -> None:
        """`window`: a session is followed while its last event is this recent."""
        self._api = api
        self._store = store
        self._max_streams = max_streams
        self._window = window
        self._retry_wait = retry_wait or wait_exponential_jitter(initial=1, max=_RETRY_MAX_WAIT)
        self._streams: dict[str, asyncio.Task[None]] = {}
        # `sequence_num` each followed session is stored up to; absent until its first page catch-up.
        self._positions: dict[str, int] = {}
        self._problems: dict[str, LiveProblem] = {}
        self._follow_set_stale = asyncio.Event()
        self._watching = False
        self.last_event_at: datetime | None = None

    @property
    def watching(self) -> bool:
        """A session watch is open, or being reopened."""
        return self._watching

    @property
    def streams(self) -> int:
        return len(self._streams)

    @property
    def problems(self) -> list[LiveProblem]:
        """What is failing now, most recent first; an entry goes once its source works again."""
        return sorted(self._problems.values(), key=lambda problem: problem.at, reverse=True)

    def _resolved(self, source: str) -> None:
        self._problems.pop(source, None)

    def refresh(self) -> None:
        """List the newest sessions and re-derive the followed set now: the stored metadata has changed."""
        self._follow_set_stale.set()

    async def run(self) -> None:
        async with asyncio.TaskGroup() as group:
            group.create_task(self._keep_watching(group))
            await self._keep_following(group)

    def _retrying(self, source: str) -> AsyncRetrying:
        """Retry every failure but a lost resume point, with the wait growing until a pass completes."""

        def log_retry(state: RetryCallState) -> None:
            failure = state.outcome.exception() if state.outcome else None
            message = describe_failure(failure) if failure else "unknown failure"
            self._problems[source] = LiveProblem(source=source, at=datetime.now(UTC), message=message)
            logger.warning("%s: retrying in %.0fs after %s", source, state.upcoming_sleep, message)

        return AsyncRetrying(wait=self._retry_wait, retry=retry_if_exception(_is_retryable), before_sleep=log_retry)

    async def _keep_watching(self, group: asyncio.TaskGroup) -> None:
        while True:
            try:
                async for attempt in self._retrying(WATCH):
                    with attempt:
                        # Every connection takes a fresh token: the server answers an old one with a watch that stays
                        # open and delivers nothing. Changes in the gap reach the store through discovery, or the next
                        # polling cycle.
                        await self._watch(await self._api.resume_token(), group)
            except ResumePointLostError as lost:
                logger.info("%s; taking a fresh resume token", lost)

    async def _watch(self, resume_token: str, group: asyncio.TaskGroup) -> None:
        self._watching = True
        try:
            async with contextlib.aclosing(
                self._api.watch_sessions(resume_token, on_connected=lambda: self._resolved(WATCH))
            ) as changes:
                async for change in changes:
                    if isinstance(change, SessionSummary):
                        await self._store.upsert_sessions([change])
                        await self._match_streams_to_store(group)
                    else:
                        logger.info("session %s was removed upstream; its stored rows stay", change.id)
        finally:
            self._watching = False

    async def _keep_following(self, group: asyncio.TaskGroup) -> None:
        while True:
            self._follow_set_stale.clear()
            async for attempt in self._retrying(DISCOVERY):
                with attempt:
                    await self._store.upsert_sessions(await self._api.recent_sessions(self._max_streams))
                    await self._match_streams_to_store(group)
            self._resolved(DISCOVERY)
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout(DISCOVERY_INTERVAL):
                    await self._follow_set_stale.wait()

    async def _match_streams_to_store(self, group: asyncio.TaskGroup) -> None:
        wanted = set(
            await self._store.live_session_ids(active_since=datetime.now(UTC) - self._window, limit=self._max_streams)
        )
        for session_id in wanted - self._streams.keys():
            self._streams[session_id] = group.create_task(self._follow(session_id))
        for session_id in self._streams.keys() - wanted:
            self._streams.pop(session_id).cancel()
            self._positions.pop(session_id, None)
            self._resolved(session_id)

    async def _follow(self, session_id: str) -> None:
        while True:
            try:
                async for attempt in self._retrying(session_id):
                    with attempt:
                        await self._tail(session_id)
            except ResumePointLostError as lost:
                logger.info("%s; paging to catch up", lost)
                self._positions.pop(session_id, None)

    async def _tail(self, session_id: str) -> None:
        """One connection: catch up by paging if the session has no position yet, then store what is pushed."""
        if session_id not in self._positions:
            after = await self._store.resume_after(session_id)
            self._positions[session_id] = await read_events_after(self._api, self._store, session_id, after=after)
        async with contextlib.aclosing(
            self._api.stream_events(
                session_id, after=self._positions[session_id], on_connected=lambda: self._resolved(session_id)
            )
        ) as events:
            async for event in events:
                position = self._positions[session_id]
                if event.seq > position + 1:
                    raise ResumePointLostError(
                        f"{session_id=}: a frame for sequence_num {event.seq} follows {position}"
                    )
                await self._store.append_events(session_id, [event])
                self._positions[session_id] = max(position, event.seq)
                self.last_event_at = datetime.now(UTC)
