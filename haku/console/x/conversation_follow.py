"""Following a conversation: its state, and the updates to it, as one operation.

A follower asks for a conversation and gets a snapshot naming the `event_seq` it was read at,
then every change after it. Initial load and resume are the same call — a reconnect passes the
position the last message carried — so there is no second way of asking and nothing for a caller
to combine.

**The ordering lives here, not in the caller.** Registering for wakes happens before the snapshot
is read, so a change landing between the two cannot be missed. It costs a flag rather than a
buffer: the wake carries no payload, and the read that follows it is positional, so an event that
arrives during the snapshot is either already in it (an idempotent duplicate) or comes in the first
update. A caller that assembled a read and a subscription itself would have to get that window
right; here there is nothing to get wrong.

**A position that cannot be served is answered with a snapshot, not an error.** A log that no
longer holds the position, and more having moved than one update should carry, recover the same
way, so the follower is simply sent the conversation whole again — which is why there is no 410 on
this path and no repair branch in a client.

**What crosses replicas is still an id.** `pg_notify` carries `{kind, session_id}` and nothing
else, capped at 8000 bytes by Postgres and using about seventy. `LISTEN` is broadcast, so the
replica holding a follower's socket hears every session's wake and reads that conversation's rows
itself. Nothing about the payload rides the notification.

**Coalescing bounds the open message.** `session_messages.content` is mutated in place as prose
arrives and a `TextDelta` is deliberately not a row, so every pass re-sends the message being
written, whole. Sending on each wake would therefore cost bytes quadratic in a turn's prose and one
read per delta per follower; `COALESCE_WINDOW` bounds both, at the price of prose reaching a tab in
half-second steps rather than per token.

**Everything a follower is shown moves, or it would not be here.** The transcript arrives
incrementally because it is the only part that grows without bound; the attachments, the earlier
sessions and the live session's own row are a handful of rows sent whole every time. A field that
only ever arrived in the snapshot would be one a tab could never be told had changed, which is a
UI quietly disagreeing with the database rather than a saving.

That obliges the writers: **anything that changes what a conversation shows must notify `UPDATE`**,
because this loop reads when it is woken and at no other time. `SessionStore.narrate` is the
example to copy — it records the row, then announces it.

**The sandbox is the exception, and is polled rather than awaited.** What Kubernetes says about a
claim, a pod and a runner is an observation of another system: no `session_events` row is written
when a pod goes ready, so no wake exists to carry it. While the session a follower is watching is
still coming up, this loop therefore re-reads on `SANDBOX_POLL` as well as on wakes — which is what
the browser's own polling used to do, at the same rate the observation cache already bounds.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncGenerator, AsyncIterator, Iterator
from datetime import timedelta
from typing import Annotated, Protocol, cast
from uuid import UUID

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect

from haku.console import operator_auth
from haku.console.chat_models import SessionStatus
from haku.console.console_events import OPERATOR_SESSION_EXPIRED_CLOSE_CODE
from haku.console.x.sandbox_claims import ClaudeSandboxProvisioningView
from haku.console.x.session_notifications import SessionEventKind, SessionNotifications
from haku.console.x.session_store import PositionUnusableError, SessionStore
from haku.console.x.session_views import (
    UPDATE_ROW_LIMIT,
    ConversationFollowMessage,
    ConversationSnapshot,
    ConversationView,
)

logger = logging.getLogger(__name__)
router = APIRouter(tags=["conversations"])

# How often a follower's operator session is re-checked. The socket is send-only, so nothing else
# would ever notice the deadline pass.
SESSION_REVALIDATION = timedelta(seconds=30)

# How long changes to one conversation pile up before its followers are sent one update.
#
# What it bounds is the open message: prose arrives per delta, the row carrying it is rewritten in
# place, and every update re-sends that row whole — so a window near zero would spend bytes
# quadratic in the length of a turn's answer and a database read per delta per follower. Half a
# second keeps prose reading as live while bounding both.
COALESCE_WINDOW = timedelta(milliseconds=500)

# How often a follower watching a session that is still coming up re-reads the cluster's account of
# its sandbox, having no wake to wait for. `SessionService` reuses one observation for
# `OBSERVATION_TTL`, so reading faster than that would return the same answer.
SANDBOX_POLL = timedelta(seconds=2)


class ConversationReader(Protocol):
    """The conversation as every surface reads it, and the sandbox view that goes with it.

    A port rather than the service itself, so a follow needs nothing from the Claude runtime but
    these two answers — and so the snapshot a follower opens with is, by construction, the same
    read `GET /api/conversations/{id}` returns rather than a second assembly of one.
    """

    async def conversation(self, operator_id: UUID, conversation_id: UUID) -> ConversationView: ...

    async def provisioning_of(
        self, session_id: UUID, status: SessionStatus
    ) -> ClaudeSandboxProvisioningView | None: ...


class ConversationFollow:
    """The follow operation, and this replica's map from a session's wake to its conversation.

    One per process, started with the app: it holds the `UPDATE` watch that every follower's wakes
    come through, so a conversation nobody is following costs a set membership test.
    """

    def __init__(
        self,
        store: SessionStore,
        reader: ConversationReader,
        notifications: SessionNotifications,
        *,
        window: timedelta = COALESCE_WINDOW,
        sandbox_poll: timedelta = SANDBOX_POLL,
    ) -> None:
        self._store = store
        self._reader = reader
        self._notifications = notifications
        self._window = window
        self._sandbox_poll = sandbox_poll
        self._followers: dict[UUID, set[asyncio.Event]] = {}
        self._woken: set[UUID] = set()
        self._pending = asyncio.Event()
        self._conversations: dict[UUID, UUID] = {}

    @contextlib.asynccontextmanager
    async def run(self) -> AsyncIterator[None]:
        with self._notifications.watch(SessionEventKind.UPDATE, self._record):
            dispatching = asyncio.create_task(self._dispatch_loop())
            try:
                yield
            finally:
                dispatching.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await dispatching

    async def follow(
        self, operator_id: UUID, conversation_id: UUID, *, after: int | None = None
    ) -> AsyncGenerator[ConversationFollowMessage]:
        """This conversation as it is now, and as it changes, until the caller stops reading.

        *after* is a position a previous message carried; absent means this follower has never read
        the conversation and is sent a snapshot. A position this log cannot answer from is sent one
        too, so a caller has one rule: a snapshot replaces what it holds, an update merges into it.

        Raises `KeyError` for a conversation this operator does not own — the check is the store's
        own read, so a follow can see exactly what a read of the same conversation can.
        """
        with self._registered(conversation_id) as woken:
            # Registered before the first read, so a change landing during it wakes this follower
            # rather than being missed. A wake it did not need costs one read.
            if after is None:
                first: ConversationFollowMessage = await self._snapshot(
                    operator_id, conversation_id, await self._store.conversation_position(conversation_id)
                )
            else:
                # A reconnect is told what it missed at once rather than on the next change, which
                # is also what makes a conversation this operator does not own fail here — where a
                # caller is still asking — rather than as a socket that never says anything.
                first = await self._caught_up(operator_id, conversation_id, after)
            position = first.position
            yield first
            message = first
            while True:
                await self._until_something_to_read(woken, message)
                # Sleep first, so changes arriving inside the window join this pass rather than
                # each buying one of their own.
                await asyncio.sleep(self._window.total_seconds())
                woken.clear()
                message = await self._caught_up(operator_id, conversation_id, position)
                position = message.position
                yield message

    async def _until_something_to_read(self, woken: asyncio.Event, last: ConversationFollowMessage) -> None:
        """Wait for this conversation to move — or, while its sandbox is coming up, for a poll.

        A wake means a row was written. Nothing writes one when Kubernetes moves a pod, so the one
        part of what a follower shows that lives in another system is the one part it has to go and
        ask about.
        """
        if _still_coming_up(last):
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout(self._sandbox_poll.total_seconds()):
                    await woken.wait()
            return
        await woken.wait()

    async def _caught_up(self, operator_id: UUID, conversation_id: UUID, position: int) -> ConversationFollowMessage:
        """What this follower is owed from *position* — an update, or the conversation whole.

        The fallback is the whole thing rather than an error because both reasons a position cannot
        be served recover the same way, and one of them (too much has moved) means the update would
        have carried most of a snapshot anyway.
        """
        try:
            update = await self._store.read_operator_conversation_changes(
                operator_id, conversation_id, after=position, limit=UPDATE_ROW_LIMIT
            )
        except PositionUnusableError:
            return await self._snapshot(
                operator_id, conversation_id, await self._store.conversation_position(conversation_id)
            )
        sandbox = await self._reader.provisioning_of(update.session_id, update.status)
        return update.model_copy(update={"provisioning": sandbox})

    async def _snapshot(self, operator_id: UUID, conversation_id: UUID, position: int) -> ConversationSnapshot:
        """The conversation whole, at a position its caller read **before** these rows.

        That order is what makes a snapshot safe to resume from: a row written between the two
        reads is carried by the follower's next update rather than by neither.
        """
        return ConversationSnapshot(
            position=position, conversation=await self._reader.conversation(operator_id, conversation_id)
        )

    @contextlib.contextmanager
    def _registered(self, conversation_id: UUID) -> Iterator[asyncio.Event]:
        woken = asyncio.Event()
        self._followers.setdefault(conversation_id, set()).add(woken)
        try:
            yield woken
        finally:
            followers = self._followers.get(conversation_id)
            if followers is not None:
                followers.discard(woken)
                if not followers:
                    del self._followers[conversation_id]

    def _record(self, session_id: UUID) -> None:
        """Note that this session moved. Runs on the listener's reader task: no awaiting here."""
        self._woken.add(session_id)
        self._pending.set()

    async def _dispatch_loop(self) -> None:
        while True:
            await self._pending.wait()
            self._pending.clear()
            woken, self._woken = self._woken, set()
            try:
                await self._dispatch(woken)
            except Exception:
                # One unresolvable id must not take the loop, and with it every other follower,
                # down: a follower that is not woken is stale, not wrong.
                logger.exception("failed to dispatch session wakes to followers")

    async def _dispatch(self, woken: set[UUID]) -> None:
        if not self._followers:
            return
        for session_id in woken:
            try:
                conversation_id = await self._conversation_of(session_id)
            except KeyError:
                # The id came off a broadcast channel, so it can name a session this database no
                # longer has. There is nobody following what it belonged to.
                logger.warning("a session update named an unknown session: %s", session_id)
                continue
            for follower in self._followers.get(conversation_id, ()):
                follower.set()

    async def _conversation_of(self, session_id: UUID) -> UUID:
        """Which thread this session's wake belongs to.

        Resolved once per session rather than once per wake, which is the other thing the window
        above is worth: a streaming turn wakes this loop constantly and a session's conversation is
        written when the row is created and never updated, so the cache needs no invalidation.
        """
        if (cached := self._conversations.get(session_id)) is None:
            cached = self._conversations[session_id] = await self._store.conversation_of(session_id)
        return cached


def _still_coming_up(message: ConversationFollowMessage) -> bool:
    """Whether the session this message describes is still waiting on its sandbox."""
    status = message.conversation.session.status if isinstance(message, ConversationSnapshot) else message.status
    return status == SessionStatus.PROVISIONING


def _follow(websocket: WebSocket) -> ConversationFollow | None:
    return cast(ConversationFollow | None, websocket.app.state.conversation_follow)


@router.websocket("/api/conversations/{conversation_id}/follow")
async def follow_conversation(
    websocket: WebSocket,
    conversation_id: UUID,
    actor: operator_auth.OperatorActorDep,
    after: Annotated[int | None, Query(ge=0, description="A `position` an earlier message carried.")] = None,
) -> None:
    """One socket per followed conversation, carrying that conversation's messages and nothing else.

    The connection **is** the subscription, which is what keeps this a wire adapter: what to send
    is `ConversationFollow.follow`, and the socket neither holds a position nor decides when to
    read. A tab following two conversations opens two of these, the same shape a `watch` takes
    everywhere else.

    Send-only. The client says what it wants in the URL and nothing after, so there is no inbound
    protocol to parse and a socket cannot be talked into reading anything its operator does not own.
    """
    if not operator_auth.exact_operator_origin(websocket):
        await websocket.close(code=1008, reason="invalid websocket origin")
        return
    following = _follow(websocket)
    if following is None:
        # The same answer `GET /api/conversations/{id}` gives on such a replica, in this transport's
        # vocabulary: 1013 is "try again later", which is what a client reconnecting should do.
        await websocket.close(code=1013, reason="the session runtime is not configured")
        return
    messages = following.follow(actor.operator_id, conversation_id, after=after)
    await websocket.accept()
    expiry = asyncio.create_task(_close_when_expired(websocket))
    try:
        async for message in messages:
            await websocket.send_json(message.model_dump(mode="json"))
    except KeyError:
        # Raised by the first read rather than by `follow` itself, which only builds a generator:
        # a conversation this operator does not own, or one deleted while it was being followed.
        await websocket.close(code=1008, reason="conversation not found")
    except WebSocketDisconnect:
        pass
    finally:
        expiry.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await expiry
        await messages.aclose()


async def _close_when_expired(websocket: WebSocket) -> None:
    """Close a follower whose operator session has reached its deadline.

    A send-only socket never reads, so nothing else would notice: the deadline is signed into the
    cookie and this is what turns it into the close code the shell re-authenticates on instead of
    treating the channel as merely offline.
    """
    while True:
        await asyncio.sleep(SESSION_REVALIDATION.total_seconds())
        if operator_auth.signed_operator_session(websocket) is None:
            await websocket.close(code=OPERATOR_SESSION_EXPIRED_CLOSE_CODE, reason="operator session expired")
            return
        if await operator_auth.operator_session(websocket) is None:
            await websocket.close(code=1008, reason="operator is disabled or missing")
            return
