"""The console's Matrix sync loop.

Logs in as the bot, long-polls `/sync`, binds the one room Haku services (R3.6a), and
hands what the operator types to the session behind that room.

**Every pass acknowledges what it read.** A batch the session will not take is rejected rather
than held: the operator is told so and sends it again, so nothing queues behind a running turn and
the loop keeps one position instead of two. An event Haku has no way to read — a screenshot, a
voice memo — is the same shape, and always was, because re-offering one could never converge.

Both are **recorded in the transaction that advances the watermark**, as `session_events` rows the
room notice is a rendering of. Advancing first and announcing afterwards would let one crash lose
the message and the notice together, which is the whole of what "nothing is silently dropped"
(R1.6) rules out.

It is also the only holder of a Matrix credential, so the supervisor's lifecycle notices go out
through `announce` rather than a second login, and an answer — a row until it has been said — is
drained into the room from here (`outbox`).

The one thing it is asked *for* rather than told is this room's recent conversation
(`recent_history`), answered out of the console's own transcript. Still this object's to answer
because it knows which room is bound; the credential has nothing to do with it.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime
import logging
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from uuid import UUID

from pydantic import SecretStr
from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from haku.console.chat_models import ChatMessageRole, PromptRejection
from haku.console.config import MatrixConfig
from haku.console.database_schema import MatrixAccessToken, MatrixSyncWatermark, SessionEvent
from haku.console.x.channels.matrix.client import (
    EventTag,
    InboundMessage,
    Invite,
    MatrixAuthError,
    MatrixClient,
    RoomEventKind,
    UnmappableEvent,
)
from haku.console.x.channels.matrix.outbox import PendingReply, RoomOutbox, RoomOutboxDrain
from haku.console.x.channels.matrix.pacer import RoomPacer
from haku.console.x.channels.matrix.session import (
    MatrixConversationStore,
    MatrixTurns,
    PromptAccepted,
    PromptRejected,
    RoomTranscript,
)
from haku.console.x.system_prompt import HistoryMessage

logger = logging.getLogger(__name__)

# Distinct from the OAuth refresh lock in oauth_association_maintenance.
_SYNC_ADVISORY_LOCK = 0x4D58_5359  # "MXSY"

# How long a replica that lost the election waits before trying again.
LEADER_RETRY = datetime.timedelta(seconds=30)
# Backoff after a failed sync, so a homeserver outage does not become a hot loop.
ERROR_BACKOFF = datetime.timedelta(seconds=10)


def _why_not(reason: PromptRejection) -> str:
    """What a rejection notice says the operator is waiting for.

    The channel's own rendering of `PromptRejection`, which is why it is here and not beside the
    enum — and a match rather than a lookup, so a member added later fails the type check instead
    of the send.
    """
    match reason:
        case PromptRejection.NO_SESSION:
            return "there is no session behind this room yet"
        case PromptRejection.SESSION_NOT_READY:
            return "Haku's sandbox is not up yet"
        case PromptRejection.TURN_IN_FLIGHT:
            return "Haku is still working on the previous message"
        case PromptRejection.PROMPT_QUEUED:
            return "a message is already waiting to be answered"


class MatrixSyncStore:
    """Durable sync state, a table per owner: the token we cached and the watermark we reached.

    Each has one writer and a `NOT NULL` value, so an absent row says one definite thing —
    nothing cached, nothing finished with.
    """

    def __init__(self, sessions: async_sessionmaker[AsyncSession]):
        self._sessions = sessions

    async def cached_token(self, user_id: str) -> str | None:
        async with self._sessions() as db:
            # Annotated because `AsyncSession.scalar` is typed `Any`, which `warn_return_any` refuses.
            token: str | None = await db.scalar(
                select(MatrixAccessToken.access_token).where(MatrixAccessToken.user_id == user_id)
            )
            return token

    async def save_token(self, user_id: str, token: str) -> None:
        """Cache the access token.

        Upserted rather than read-then-inserted because the pacer's queue runs on every replica,
        so two of them can log in and first-write this row at once; the loser of a read-then-insert
        fails on the primary key, and on the pacer's side that is a queued send lost.
        """
        async with self._sessions() as db, db.begin():
            await db.execute(
                insert(MatrixAccessToken)
                .values(user_id=user_id, access_token=token)
                .on_conflict_do_update(index_elements=["user_id"], set_={"access_token": token})
            )

    async def watermark(self, user_id: str) -> str | None:
        async with self._sessions() as db:
            # Annotated for the same reason `cached_token` is.
            reached: str | None = await db.scalar(
                select(MatrixSyncWatermark.next_batch).where(MatrixSyncWatermark.user_id == user_id)
            )
            return reached

    async def advance(self, user_id: str, next_batch: str, events: Sequence[SessionEvent] = ()) -> None:
        """Acknowledge everything up to *next_batch*, recording what the pass decided about it.

        **One transaction, and that is the point.** `events` are the facts about the batch that
        exist nowhere else — a prompt this pass rejected, an attachment it could not read — and
        each is the durable half of a notice the room is about to be told. Committing the
        watermark first would let a crash acknowledge a message to the homeserver while losing
        both the record of it and the operator's only account of what happened to it.

        The watermark is what makes an outage replay rather than skip (R1.7), so what is written
        here must be a position genuinely finished with.
        """
        async with self._sessions() as db, db.begin():
            db.add_all(events)
            await db.execute(
                insert(MatrixSyncWatermark)
                .values(user_id=user_id, next_batch=next_batch)
                .on_conflict_do_update(index_elements=["user_id"], set_={"next_batch": next_batch})
            )


class MatrixSyncService:
    """Runs `/sync` on whichever replica holds the advisory lock."""

    def __init__(
        self,
        config: MatrixConfig,
        password: SecretStr,
        engine: AsyncEngine,
        store: MatrixSyncStore,
        conversations: MatrixConversationStore,
        turns: MatrixTurns,
        transcript: RoomTranscript,
        outbox: RoomOutbox,
    ):
        # Taken separately from `config`, which carries it as optional: the service is
        # only ever constructed once the password is known to be there (R10.3b).
        self._config = config
        self._password = password
        self._engine = engine
        self._store = store
        self._conversations = conversations
        self._turns = turns
        self._transcript = transcript
        self._client = MatrixClient(config.homeserver, config.user_id, config.device_id)
        # Public because it has a lifecycle, and this object's owner is the one that drives it:
        # everything the console says into the room goes through here, so it outlives no
        # individual send and belongs to whoever is running the service.
        self.pacer = RoomPacer()
        # Held here rather than by the composition root because it needs the two things only this
        # object has: the credential that can speak into the room, and the pacer that decides when.
        self._outbox = RoomOutboxDrain(engine, outbox, self.pacer, self.post_reply, self.bound_room)
        self._status_event_id: str | None = None
        self._status_body: str | None = None

    async def _token(self) -> str:
        """A working access token, logging in only when the cached one is not.

        Synapse rate-limits `/login`, so re-authenticating on every pass would get the
        console throttled — hence the cache (R10.3a).
        """
        cached = await self._store.cached_token(self._config.user_id)
        if cached is not None and await self._client.whoami(cached):
            return cached
        token = await self._client.login(self._password.get_secret_value())
        await self._store.save_token(self._config.user_id, token)
        logger.info("Matrix: logged in as %s", self._config.user_id)
        return token

    async def _handle_invite(self, token: str, invite: Invite) -> None:
        """Join invites from the operator, and only into the one live room (R3.6, R3.6a)."""
        if invite.inviter != self._config.operator_user_id:
            logger.warning(
                "Matrix: leaving invite to %s from %s pending — not the operator", invite.room_id, invite.inviter
            )
            return
        if (live_room := await self._conversations.claim_room(self._config.user_id, invite.room_id)) != invite.room_id:
            # Joining would put Haku in a room nothing services, which reads as listening
            # (R3.6a). Say so where we can actually speak: the room already bound.
            logger.warning("Matrix: refusing invite to %s — already serving %s", invite.room_id, live_room)
            self._queue_notice(
                live_room, f"invited to another room; still serving this one ({live_room})", RoomEventKind.ROOM
            )
            return
        await self._client.join(token, invite.room_id)
        logger.info("Matrix: joined %s on invite from %s", invite.room_id, invite.inviter)
        self._queue_notice(invite.room_id, "joined — this is now Haku's room", RoomEventKind.ROOM)

    async def post_reply(self, reply: PendingReply) -> None:
        """Post one queued answer into the room as ordinary text (R11.1).

        Called by `RoomOutboxDrain`, from inside the pacer's queue, so this is the send itself —
        it raises when the homeserver refuses, and that is what leaves the row unsent and
        claimable again. The tag is what makes the event say which transcript row it is showing,
        and the transaction id is the row's own, so a redrive is refused rather than doubled.
        """
        await self._client.send_text(
            await self._token(), reply.room_id, reply.body, txn_id=reply.transaction_id(), tag=reply.tag()
        )

    async def bound_room(self) -> str | None:
        """The room this console services, or None before the operator has invited it into one."""
        conversation = await self._conversations.load(self._config.user_id)
        return None if conversation is None else conversation.room_id

    async def show_status(self, body: str, session_id: UUID | None = None) -> None:
        """Make the room's single status line say *body*, creating or editing it (R6.2, R6.5).

        One line per turn rather than a notice per step: a room where every tool call is a
        message is a room nobody reads. The event id of the live line is held here because
        this is the only object that knows the room and the token; the turn loop says what
        the state is and never learns how it is shown.

        **Idempotent, and paced by the room rather than by this call.** The floor moved to the
        caller (`room_status.TurnStatus`), because deciding what the line should say and deciding when it
        may change have to be one decision; the room's own budget is `pacer`'s, where
        the status line is the one sender allowed to overwrite what it has not yet said.

        Create-or-edit is decided inside the queued send, not here, because the create is what
        produces the event id the edit needs — and between queueing and sending, it may not
        have happened yet. The pacer is serial, so by the time an edit runs its create has.
        """
        if body == self._status_body:
            return
        conversation = await self._conversations.load(self._config.user_id)
        if conversation is None:
            return
        self._status_body = body
        room_id = conversation.room_id

        tag = EventTag(kind=RoomEventKind.STATUS, session_id=session_id)

        async def post() -> None:
            token = await self._token()
            if self._status_event_id is None:
                self._status_event_id = await self._client.send_notice(
                    token, room_id, body, txn_id=tag.transaction_id(), tag=tag
                )
                return
            await self._client.edit_notice(
                token, room_id, self._status_event_id, body, txn_id=tag.transaction_id(), tag=tag
            )

        self.pacer.set_status(post)

    async def set_typing(self, active: bool) -> None:
        """Show or hide Haku's typing indicator in the live room (R6.1).

        Best effort by construction: a failed typing notice is cosmetic, and a turn that died
        because the room could not be told it was thinking would be a strictly worse outcome
        than an indicator that is briefly wrong. The homeserver expires the notice on its own,
        so the failure mode of a lost `False` is a stale indicator for seconds, not forever.
        """
        conversation = await self._conversations.load(self._config.user_id)
        if conversation is None:
            return
        try:
            await self._client.set_typing(await self._token(), conversation.room_id, active=active)
        except Exception:
            logger.warning("Matrix: typing notification failed (active=%s)", active, exc_info=True)

    async def clear_status(self) -> None:
        """Retire the status line, if one was ever created (R6.5).

        Called on every terminal path, including failure — a status line left saying "running
        Bash" after the turn died is the stuck-typing-indicator bug in another costume.

        A change still waiting to go out is dropped rather than sent and then redacted, which
        would spend two of the room's ten sends showing something for a fraction of a second.
        Reading the event id is left to the queued send for the same reason `show_status`
        does: a create queued a moment ago has not necessarily happened yet.
        """
        self._status_body = None
        self.pacer.drop_status()
        conversation = await self._conversations.load(self._config.user_id)
        if conversation is None:
            return
        room_id = conversation.room_id

        async def retire() -> None:
            event_id, self._status_event_id = self._status_event_id, None
            if event_id is None:
                return
            await self._client.redact(await self._token(), room_id, event_id, reason="turn finished")

        self.pacer.send(retire)

    async def recent_history(self, before_session: UUID, limit: int) -> tuple[HistoryMessage, ...]:
        """The tail of the live room's conversation, for re-awakening a replacement session.

        **Answered from our own transcript, not from the homeserver's copy of the room.** Matrix is
        one channel among several, so what a replacement session believes happened has to come from
        the record every channel writes into; reading it back out of `/messages` made the channel
        the source of truth for its own conversation, and a second channel — Telegram's bot API
        cannot page a chat's history — could not have reproduced that memory
        (<../../../debug/channel_write_audit.md> § "What a second channel would need", #4130).

        **A message the previous session never answered is still in here**, which is what a
        replacement is for: ingress writes the prompt row when it accepts a batch, and a session
        that ended before claiming that row leaves it recorded and unanswered. Reading the room
        would find the same event; reading our own transcript finds it whether or not the channel
        that carried it still has it.

        Empty until something has been recorded for this room, which is honestly what a room with
        no transcript has: a first-ever session and a session whose room only just bound both read
        the same, and both are correct.
        """
        conversation = await self._conversations.load(self._config.user_id)
        if conversation is None:
            return ()
        return tuple(
            HistoryMessage(
                # The one per-channel step in this path: a recorded role becomes an address, and
                # which addresses those are is the channel's own business.
                sender=self._config.operator_user_id if said.role is ChatMessageRole.USER else self._config.user_id,
                body=said.body,
                sent_at=said.sent_at,
            )
            for said in await self._transcript.recent(conversation.room_id, before_session=before_session, limit=limit)
        )

    async def announce(self, body: str, kind: RoomEventKind = RoomEventKind.LIFECYCLE) -> None:
        """Post a lifecycle notice into the live room, if there is one.

        The supervisor's outbound path (`session.Announce`): it owns session
        lifecycle but never a Matrix credential, so it speaks through the loop that has one.
        A no-op before any room is bound — there is genuinely nowhere to say it.
        """
        conversation = await self._conversations.load(self._config.user_id)
        if conversation is None:
            logger.info("Matrix: no room bound yet, dropping notice: %s", body)
            return
        self._queue_notice(conversation.room_id, body, kind)

    def _queue_notice(self, room_id: str, body: str, kind: RoomEventKind) -> None:
        tag = EventTag(kind=kind)

        async def post() -> None:
            await self._client.send_notice(await self._token(), room_id, body, txn_id=tag.transaction_id(), tag=tag)

        self.pacer.send(post)

    def _serviced[T: (InboundMessage, UnmappableEvent)](self, events: tuple[T, ...], live_room: str | None) -> list[T]:
        """The events of a batch that are ours to act on — read or report.

        Haku's own posts are not among them, and are already gone: `MatrixClient._read`
        drops everything the bot sent (R1.5). This used to check them again against a set of
        every event id this process had ever sent — a filter that could not match, since every
        entry in it was excluded one layer down, and that cost memory for as long as the replica
        lived and was empty again the moment it restarted.
        """
        serviced = []
        for event in events:
            if event.room_id != live_room:
                # Only the bound room is serviced (R3.6a). Reachable for a room joined
                # before the binding existed, and for anything that gets Haku into a room
                # by a path other than an invite.
                logger.warning("Matrix: ignoring %s from unserviced room %s", event.event_id, event.room_id)
                continue
            serviced.append(event)
        return serviced

    async def sync_once(self, token: str) -> None:
        """One `/sync` pass: act on what came back, and acknowledge it.

        The watermark always moves, because everything this pass read has been answered one way
        or the other — handed to the session, or rejected and said so. What it decided is written
        with the watermark rather than after it, so the two cannot come apart.

        **The room is told once that is committed**, since the notice is a rendering of a row and
        the pacer's queue is in-process: a crash before the commit re-delivers the batch and says
        nothing, and one after it leaves a recorded fact whose notice can be posted again.
        """
        result = await self._client.sync(token, await self._store.watermark(self._config.user_id))
        for invite in result.invites:
            await self._handle_invite(token, invite)
        # Read after the invites, so a room bound by this very batch serves it too.
        live_room = await self._live_room(token, result.messages)
        messages = self._serviced(result.messages, live_room)
        unreadable = self._serviced(result.unmappable, live_room)
        recorded = list(await self._turns.unreadable(unreadable)) if unreadable else []
        rejection: PromptRejection | None = None
        if messages:
            match await self._turns.offer(messages):
                case PromptAccepted():
                    logger.info("Matrix: handed %d message(s) to the session", len(messages))
                case PromptRejected(reason=reason, event=event):
                    rejection = reason
                    if event is not None:
                        recorded.append(event)
        await self._store.advance(self._config.user_id, result.next_batch, recorded)
        if rejection is not None:
            self._report_rejected(live_room, len(messages), rejection)
        self._report_unreadable(live_room, unreadable)

    async def _live_room(self, token: str, messages: tuple[InboundMessage, ...]) -> str | None:
        """The room being serviced, adopting one from traffic when nothing is bound.

        Membership already required an operator invite (R3.6), so a room Haku is joined to
        and being spoken to in is one the operator put it in — adopting it is recovering a
        binding, not granting access. Without this, a room joined before the binding existed
        goes quiet forever with no way for the operator to revive it from a Matrix client.
        """
        if (conversation := await self._conversations.load(self._config.user_id)) is not None:
            return conversation.room_id
        adopted = next((m.room_id for m in messages if m.sender == self._config.operator_user_id), None)
        if adopted is None:
            return None
        room = await self._conversations.claim_room(self._config.user_id, adopted)
        logger.info("Matrix: adopted %s from traffic — no room was bound", room)
        self._queue_notice(room, "adopted this room — Haku had no room bound", RoomEventKind.ROOM)
        return room

    def _report_rejected(self, live_room: str | None, count: int, reason: PromptRejection) -> None:
        """Tell the room its messages were not delivered, and what to wait for (R1.6).

        Every pass that rejects says so, because every rejection is a different message: nothing
        is re-offered, so there is no repetition to suppress.

        The reason is named, which the holding notice it replaces could not do — a hold covered a
        turn in flight, a sandbox still provisioning and no session at all with one sentence, and
        the operator's next move differs in each.
        """
        if live_room is None:
            return
        self._queue_notice(
            live_room, f"{count} message(s) not delivered — {_why_not(reason)}; send them again", RoomEventKind.REJECTED
        )

    def _report_unreadable(self, live_room: str | None, events: list[UnmappableEvent]) -> None:
        """Say in the room that something arrived which Haku has no way to read (R1.6).

        Said in the room and not only logged, because the room is where the operator is: a
        screenshot that disappears with a line in a pod's stdout is the failure R1.6 names.
        """
        if not events or live_room is None:
            return
        msgtypes = ", ".join(sorted({event.msgtype for event in events}))
        self._queue_notice(
            live_room,
            f"received {len(events)} message(s) Haku cannot read ({msgtypes}) — it reads text only; "
            "describe them in words and they will reach the session",
            RoomEventKind.UNREADABLE,
        )

    async def _run_as_leader(self) -> None:
        """Sync until cancelled. Only ever entered holding the advisory lock."""
        token = await self._token()
        while True:
            try:
                await self.sync_once(token)
            except MatrixAuthError:
                logger.warning("Matrix: access token rejected, logging in again")
                token = await self._token()
            except Exception:
                logger.exception("Matrix: sync pass failed")
                await asyncio.sleep(ERROR_BACKOFF.total_seconds())

    async def _run(self) -> None:
        """Contend for leadership, and sync for as long as we hold it.

        Unlike the OAuth refresh sweep, which takes the lock per pass, this holds it for
        the lifetime of the loop: `/sync` is a long poll, so releasing between passes
        would let two replicas interleave and double-process every batch.
        """
        while True:
            async with self._engine.connect() as leader:
                if not await leader.scalar(text("SELECT pg_try_advisory_lock(:lock)"), {"lock": _SYNC_ADVISORY_LOCK}):
                    await asyncio.sleep(LEADER_RETRY.total_seconds())
                    continue
                logger.info("Matrix: this replica is the sync leader")
                try:
                    await self._run_as_leader()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("Matrix: sync loop exited, retrying")
                    await asyncio.sleep(ERROR_BACKOFF.total_seconds())
                finally:
                    with contextlib.suppress(Exception):
                        await leader.scalar(text("SELECT pg_advisory_unlock(:lock)"), {"lock": _SYNC_ADVISORY_LOCK})

    @asynccontextmanager
    async def run(self) -> AsyncIterator[None]:
        """Hold the sync loop, the room's outbound queue and its outbox drain.

        The pacer runs on **every** replica, not only the sync leader: the console's narration is
        queued from whichever replica holds the session's lease, which is not generally the one
        holding the sync lock. That is also why the budget it enforces is an estimate — see
        `pacer`. The drain contends for a lock of its own, so it runs on one replica while
        the pacer runs on all of them, which is what keeps replies in order.
        """
        task = asyncio.create_task(self._run(), name="matrix-sync")
        try:
            async with self.pacer.run(), self._outbox.run():
                yield
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            await self._client.close()
