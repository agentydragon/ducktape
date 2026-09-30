"""Follow the sessions that are moving through the server's push streams (design: docs/sync.md § Live following).

Two streams feed the mirror between polling cycles: the session watch names a session as it appears or changes,
and one event stream per recently active session delivers its events as the server stores them. Both only ever
add to what a polling cycle would read, and every stream event goes through the same contiguity rule: an event
past a gap is never stored, the session is paged instead.
"""

import asyncio
import contextlib
import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from tenacity import AsyncRetrying, RetryCallState, retry_if_exception, wait_exponential_jitter
from tenacity.wait import wait_base

from devinfra.claude.session_export.api import ResumePointLostError, SessionsApi, WatchCursor
from devinfra.claude.session_export.models import SessionSummary
from devinfra.claude.session_export.store import SessionStore
from devinfra.claude.session_export.sync import read_events_after

logger = logging.getLogger(__name__)

# How often the sessions worth following are re-derived when nothing prompts it: this is what retires a session
# whose last event has aged out of the window.
FOLLOW_SET_INTERVAL = 60.0
_RETRY_MAX_WAIT = 300.0


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
        on_resync: Callable[[], None],
        retry_wait: wait_base | None = None,
    ) -> None:
        """`window`: a session is followed while its last event is this recent. `on_resync`: called when the watch
        lost its place, so changes may have gone unseen and only a list will show them."""
        self._api = api
        self._store = store
        self._max_streams = max_streams
        self._window = window
        self._on_resync = on_resync
        self._retry_wait = retry_wait or wait_exponential_jitter(initial=1, max=_RETRY_MAX_WAIT)
        self._streams: dict[str, asyncio.Task[None]] = {}
        # `sequence_num` each followed session is stored up to; absent until its first page catch-up.
        self._positions: dict[str, int] = {}
        self._follow_set_stale = asyncio.Event()
        self._watching = False
        self.last_frame_at: datetime | None = None

    @property
    def watching(self) -> bool:
        """A session watch is open, or being reopened."""
        return self._watching

    @property
    def streams(self) -> int:
        return len(self._streams)

    def refresh(self) -> None:
        """Re-derive the sessions to follow now: the stored metadata has changed."""
        self._follow_set_stale.set()

    async def run(self) -> None:
        async with asyncio.TaskGroup() as group:
            group.create_task(self._keep_watching())
            group.create_task(self._keep_following(group))

    def _retrying(self, label: str) -> AsyncRetrying:
        """Retry every failure but a lost resume point, with the wait growing until a pass completes."""

        def log_retry(state: RetryCallState) -> None:
            failure = state.outcome.exception() if state.outcome else None
            logger.warning("%s: retrying in %.0fs after %r", label, state.upcoming_sleep, failure)

        return AsyncRetrying(wait=self._retry_wait, retry=retry_if_exception(_is_retryable), before_sleep=log_retry)

    async def _keep_watching(self) -> None:
        cursor: WatchCursor | None = None
        lost_place = False
        while True:
            try:
                async for attempt in self._retrying("session watch"):
                    with attempt:
                        if cursor is None:
                            cursor = WatchCursor(await self._api.resume_token())
                            if lost_place:
                                # Asked after the token is taken, so the list it triggers covers the gap.
                                self._on_resync()
                                lost_place = False
                        await self._watch(cursor)
            except ResumePointLostError as lost:
                logger.info("%s; taking a fresh resume token", lost)
                cursor = None
                lost_place = True

    async def _watch(self, cursor: WatchCursor) -> None:
        self._watching = True
        try:
            async with contextlib.aclosing(self._api.watch_sessions(cursor)) as changes:
                async for change in changes:
                    self.last_frame_at = datetime.now(UTC)
                    if isinstance(change, SessionSummary):
                        await self._store.upsert_sessions([change])
                    else:
                        logger.info("session %s was removed upstream; its stored rows stay", change.id)
                    self.refresh()
        finally:
            self._watching = False

    async def _keep_following(self, group: asyncio.TaskGroup) -> None:
        while True:
            async for attempt in self._retrying("follow set"):
                with attempt:
                    await self._match_streams_to_store(group)
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout(FOLLOW_SET_INTERVAL):
                    await self._follow_set_stale.wait()

    async def _match_streams_to_store(self, group: asyncio.TaskGroup) -> None:
        self._follow_set_stale.clear()
        wanted = set(
            await self._store.live_session_ids(active_since=datetime.now(UTC) - self._window, limit=self._max_streams)
        )
        for session_id in wanted - self._streams.keys():
            self._streams[session_id] = group.create_task(self._follow(session_id))
        for session_id in self._streams.keys() - wanted:
            self._streams.pop(session_id).cancel()
            self._positions.pop(session_id, None)

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
            self._api.stream_events(session_id, after=self._positions[session_id])
        ) as events:
            async for event in events:
                position = self._positions[session_id]
                if event.seq > position + 1:
                    raise ResumePointLostError(
                        f"{session_id=}: a frame for sequence_num {event.seq} follows {position}"
                    )
                await self._store.append_events(session_id, [event])
                self._positions[session_id] = max(position, event.seq)
                self.last_frame_at = datetime.now(UTC)
