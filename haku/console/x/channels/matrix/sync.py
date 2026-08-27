"""The console's Matrix sync loop.

Logs in as the bot, long-polls `/sync`, binds the one room Haku services, and hands what the
operator types to the conversation attached to that room.

**Every pass acknowledges what it read.** A batch the session will not take is rejected rather than
held: the operator is told so and sends it again, so nothing queues behind a running turn and the
loop keeps one position instead of two. An event Haku has no way to read — a screenshot, a voice
memo — is the same shape, because re-offering one could never converge.

Both are **recorded in the transaction that advances the watermark**, as `conversation_event` rows
the room notice is a rendering of. Advancing first and announcing afterwards would let one crash
lose the message and the notice together.

An accepted batch is the one thing that commits *before* the watermark, so a crash in between
re-delivers it. That is what `ingress_ledger` is for: the loop asks the record which events a
prompt already carries rather than trusting its own position.

The same `/sync` also carries Haku's own events back. Ingress drops them; the mirror reader keeps
them (`room_copy`) — recorded ahead of the watermark, never treated as input — and a second live
copy of one projected notice is redacted here, where the credential is.

It is also the only holder of a Matrix credential. Channel-owned notices go out through `announce`
rather than a second login; runtime lifecycle is projected from durable conversation events. An
answer — a row until it has been said — is drained into the room from here (`outbox`).

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

from haku.console.config import MatrixConfig
from haku.console.database_schema import MatrixAccessToken, MatrixSyncWatermark
from haku.console.operator_identity_store import PostgresOperatorIdentityStore
from haku.console.x.channels.matrix.client import (
    ConversationEventSource,
    EventTag,
    InboundMessage,
    Invite,
    MatrixAuthError,
    MatrixClient,
    ProjectedEvent,
    Redaction,
    RoomEventKind,
    UnmappableEvent,
)
from haku.console.x.channels.matrix.conversation import (
    ConversationFacts,
    MatrixConversationStore,
    MatrixTurns,
    PromptAccepted,
    PromptRejected,
)
from haku.console.x.channels.matrix.ingress_ledger import IngressLedger
from haku.console.x.channels.matrix.outbox import PendingReply, RoomOutbox, RoomOutboxDrain
from haku.console.x.channels.matrix.pacer import RoomPacer
from haku.console.x.channels.matrix.revisions import RevisionLog
from haku.console.x.channels.matrix.room_copy import RoomCopy
from haku.console.x.conversation_log import writer_for

logger = logging.getLogger(__name__)

# Distinct from the OAuth refresh lock in oauth_association_maintenance.
_SYNC_ADVISORY_LOCK = 0x4D58_5359  # "MXSY"

# What the room's one status line is called in `matrix_revision`. Unparameterised because the room
# shows one at a time: retiring the line frees the subject, and the next turn's creates it again.
STATUS_SUBJECT = "status"

# How long a replica that lost the election waits before trying again.
LEADER_RETRY = datetime.timedelta(seconds=30)
# Backoff after a failed sync, so a homeserver outage does not become a hot loop.
ERROR_BACKOFF = datetime.timedelta(seconds=10)


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

        Upserted rather than read-then-inserted because the pacer's queue runs on every replica, so
        two of them can log in and first-write this row at once; the loser of a read-then-insert
        fails on the primary key, which on the pacer's side is a queued send lost.
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

    async def advance(self, user_id: str, next_batch: str, facts: ConversationFacts | None = None) -> None:
        """Acknowledge everything up to *next_batch*, recording what the pass decided about it.

        **One transaction.** `facts` are what this pass decided that exists nowhere else — a prompt
        it rejected, an attachment it could not read — and each is the durable half of a notice the
        room is about to be told. Committing the watermark first would let a crash acknowledge a
        message to the homeserver while losing both the record of it and the operator's only account
        of what happened to it.

        The watermark is what makes an outage replay rather than skip, so what is written here must
        be a position genuinely finished with.
        """
        async with self._sessions() as db, db.begin():
            if facts is not None and facts.bodies:
                writer = await writer_for(
                    db,
                    facts.conversation_id,
                    session_id=facts.session_id,
                    turn_id=None,
                    now=datetime.datetime.now(datetime.UTC),
                )
                for body in facts.bodies:
                    writer.authored(body)
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
        identities: PostgresOperatorIdentityStore,
        turns: MatrixTurns,
        outbox: RoomOutbox,
        revisions: RevisionLog,
        ledger: IngressLedger,
        room_copy: RoomCopy,
    ):
        # Taken separately from `config`, which carries it as optional: the service is only ever
        # constructed once the password is known to be there.
        self._config = config
        self._password = password
        self._engine = engine
        self._store = store
        self._conversations = conversations
        self._identities = identities
        self._turns = turns
        self._revisions = revisions
        self._ledger = ledger
        self._room_copy = room_copy
        self._client = MatrixClient(config.homeserver, config.user_id, config.device_id)
        # Public because its lifecycle is the owner's to drive: everything the console says into
        # the room goes through here, so it outlives no individual send.
        self.pacer = RoomPacer()
        # Held here rather than by the composition root: it needs the credential that can speak
        # into the room and the pacer that decides when, which only this object has.
        self._outbox = RoomOutboxDrain(engine, outbox, self.pacer, self.post_reply, self.bound_room)
        self._status_body: str | None = None

    async def _token(self) -> str:
        """A working access token, logging in only when the cached one is not.

        Synapse rate-limits `/login`, so re-authenticating on every pass would get the console
        throttled — hence the cache.
        """
        cached = await self._store.cached_token(self._config.user_id)
        if cached is not None and await self._client.whoami(cached):
            return cached
        token = await self._client.login(self._password.get_secret_value())
        await self._store.save_token(self._config.user_id, token)
        logger.info("Matrix: logged in as %s", self._config.user_id)
        return token

    async def _operator_id(self) -> UUID:
        """The canonical Operator behind the configured MXID.

        Resolved per call rather than cached at startup: the console comes up with the Matrix
        surface configured even where identity resolution is not yet possible, and a cached
        failure would never recover.
        """
        return await self._identities.resolve_configured_external_user_key(self._config.operator_subject)

    async def _handle_invite(self, token: str, invite: Invite) -> None:
        """Join invites from the operator, and only into the one live room."""
        if invite.inviter != self._config.operator_user_id:
            logger.warning(
                "Matrix: leaving invite to %s from %s pending — not the operator", invite.room_id, invite.inviter
            )
            return
        # Binding opens the room's conversation, so this needs the Operator it belongs to. An
        # identity that cannot be resolved yet raises, leaving the invite unjoined; the homeserver
        # keeps reporting it, so the next pass tries again.
        bound = await self._conversations.bind_room(invite.room_id, await self._operator_id())
        if bound.room_id != invite.room_id:
            # Joining would put Haku in a room nothing services, which reads as listening. Say
            # so where we can actually speak: the room already bound.
            logger.warning("Matrix: refusing invite to %s — already serving %s", invite.room_id, bound.room_id)
            self._queue_notice(
                bound.room_id, f"invited to another room; still serving this one ({bound.room_id})", RoomEventKind.ROOM
            )
            return
        await self._client.join(token, invite.room_id)
        logger.info("Matrix: joined %s on invite from %s", invite.room_id, invite.inviter)
        self._queue_notice(invite.room_id, "joined — this is now Haku's room", RoomEventKind.ROOM)

    async def post_reply(self, reply: PendingReply) -> str:
        """Post one queued answer into the room as ordinary text.

        Called by `RoomOutboxDrain` from inside the pacer's queue, so this is the send itself: it
        raises when the homeserver refuses, which is what leaves the row unsent and claimable
        again. The tag says which transcript row the event is showing, and the transaction id is
        the row's own, so a redrive is refused rather than doubled.
        """
        return await self._client.send_text(
            await self._token(), reply.room_id, reply.body, txn_id=reply.transaction_id(), tag=reply.tag()
        )

    async def bound_room(self) -> str | None:
        """The room this console services, or None before the operator has invited it into one."""
        binding = await self._conversations.bound_room()
        return None if binding is None else binding.room_id

    async def show_status(self, body: str) -> None:
        """Make the room's single status line say *body*, creating or editing it.

        One line per turn rather than a notice per step: a room where every tool call is a message
        is a room nobody reads. The turn loop says what the state is and never learns how it is
        shown.

        **Idempotent, and paced by the room rather than by this call.** The floor belongs to the
        caller (`room_status.TurnStatus`), because what the line should say and when it may change
        are one decision; the room's budget is `pacer`'s, where the status line is the one sender
        allowed to overwrite what it has not yet said.

        Create-or-edit is decided inside the queued send, because the create is what produces the
        event id the edit needs and may not have happened at queue time. The pacer is serial, so by
        the time an edit runs its create has.

        **Which event to edit comes from `matrix_revision`, not from this process.** The line
        outlives the replica that posted it: whichever replica holds the session's lease drives the
        status, and an adopting one would otherwise post a second line beside its predecessor's.
        """
        if body == self._status_body:
            return
        if (binding := await self._conversations.bound_room()) is None:
            return
        room_id = binding.room_id
        if (attachment_id := await self._conversations.attachment(room_id)) is None:
            logger.warning("Matrix: %s has no live attachment, leaving the status line alone", room_id)
            return
        self._status_body = body

        tag = EventTag(kind=RoomEventKind.STATUS, conversation_id=binding.conversation_id)

        async def post() -> None:
            token = await self._token()
            if (showing := await self._revisions.live(attachment_id, STATUS_SUBJECT)) is None:
                event_id = await self._client.send_notice(token, room_id, body, txn_id=tag.transaction_id(), tag=tag)
                await self._revisions.record(attachment_id, STATUS_SUBJECT, event_id)
                return
            await self._client.edit_notice(token, room_id, showing.event_id, body, txn_id=tag.transaction_id(), tag=tag)

        self.pacer.set_status(post)

    async def set_typing(self, active: bool) -> None:
        """Show or hide Haku's typing indicator in the live room.

        Best effort by construction: a failed typing notice is cosmetic, where a turn that died
        because the room could not be told it was thinking would not be. The homeserver expires the
        notice on its own, so a lost `False` is a stale indicator for seconds, not forever.
        """
        if (binding := await self._conversations.bound_room()) is None:
            return
        try:
            await self._client.set_typing(await self._token(), binding.room_id, active=active)
        except Exception:
            logger.warning("Matrix: typing notification failed (active=%s)", active, exc_info=True)

    async def clear_status(self) -> None:
        """Retire the status line, if one was ever created.

        Called on every terminal path, including failure — a status line left saying "running Bash"
        after the turn died is the stuck-typing-indicator bug in another costume.

        A change still waiting to go out is dropped rather than sent and then redacted, which would
        spend two of the room's ten sends showing something for a fraction of a second. Reading the
        event id is left to the queued send for the same reason `show_status` does.
        """
        self._status_body = None
        self.pacer.drop_status()
        if (binding := await self._conversations.bound_room()) is None:
            return
        room_id = binding.room_id
        if (attachment_id := await self._conversations.attachment(room_id)) is None:
            return

        async def retire() -> None:
            if (showing := await self._revisions.live(attachment_id, STATUS_SUBJECT)) is None:
                return
            await self._client.redact(await self._token(), room_id, showing.event_id, reason="turn finished")
            await self._revisions.retire(showing.revision_id)

        self.pacer.send(retire)

    async def announce(self, body: str, kind: RoomEventKind = RoomEventKind.LIFECYCLE) -> None:
        """Post one channel-owned notice into the live room, if there is one.

        Runtime lifecycle is projected from durable conversation events. This direct path remains
        for Matrix's own room adoption/refusal notices. A no-op before any room is bound — there is
        genuinely nowhere to say it.
        """
        if (binding := await self._conversations.bound_room()) is None:
            logger.info("Matrix: no room bound yet, dropping notice: %s", body)
            return
        self._queue_notice(binding.room_id, body, kind)

    async def project_notice(
        self,
        room_id: str,
        attachment_id: UUID,
        body: str,
        kind: RoomEventKind,
        conversation_id: UUID,
        source_event_seq: int,
    ) -> None:
        """Post one notice derived from a durable conversation event.

        Unlike `announce`, this call does not return while the effect exists only in the pacer's
        memory. `RoomNotices` advances its cursor after this returns, so a send failure or process
        death leaves the source event owed. Replaying it uses the same Matrix transaction id.
        """
        tag = EventTag(
            kind=kind,
            source=ConversationEventSource(
                attachment_id=attachment_id, conversation_id=conversation_id, event_seq=source_event_seq
            ),
        )

        async def post() -> None:
            await self._client.send_notice(await self._token(), room_id, body, txn_id=tag.transaction_id(), tag=tag)

        await self.pacer.send_and_wait(post)

    def _queue_notice(self, room_id: str, body: str, kind: RoomEventKind) -> None:
        tag = EventTag(kind=kind)

        async def post() -> None:
            await self._client.send_notice(await self._token(), room_id, body, txn_id=tag.transaction_id(), tag=tag)

        self.pacer.send(post)

    def _serviced[T: (InboundMessage, UnmappableEvent)](self, events: Sequence[T], live_room: str | None) -> list[T]:
        """The events of a batch that are ours to act on — read or report.

        Haku's own posts are already gone: `MatrixClient._read` drops everything the bot sent.
        """
        serviced = []
        for event in events:
            if event.room_id != live_room:
                # Only the bound room is serviced. Reachable for a room joined before the
                # binding existed, and for anything that gets Haku into a room without an invite.
                logger.warning("Matrix: ignoring %s from unserviced room %s", event.event_id, event.room_id)
                continue
            serviced.append(event)
        return serviced

    async def sync_once(self, token: str) -> None:
        """One `/sync` pass: act on what came back, and acknowledge it.

        The watermark always moves, because everything this pass read has been answered one way or
        the other — handed to the session, or rejected and said so — and what it decided is written
        with the watermark rather than after it.

        **What was recorded, the room is not told here.** The row is the notice: `RoomNotices`
        renders it from the record at its own position, so this pass writes and stops. Every
        refusal reaches a row now — what a rejection is about is the conversation, which exists as
        soon as the room is bound — so a room with nowhere to record one is a room with nowhere to
        say it either.

        **A re-delivered message is dropped from the batch rather than offered again**, and what
        makes that safe is that the ledger only knows an event because a prompt in the record
        carries it.

        **The room's own copy is recorded before the watermark moves.** Recording is idempotent,
        so a crash in between re-records rather than forgets — which is what lets the reconciler
        treat "the watermark is past an echo" as "its correspondence is durable".
        """
        result = await self._client.sync(token, await self._store.watermark(self._config.user_id))
        for invite in result.invites:
            await self._handle_invite(token, invite)
        # Read after the invites, so a room bound by this very batch serves it too.
        live_room = await self._live_room(token, result.messages)
        await self._record_own_copy(result.projected, result.redactions, live_room)
        messages = await self._undelivered(self._serviced(result.messages, live_room))
        unreadable = self._serviced(result.unmappable, live_room)
        recorded = await self._turns.unreadable(unreadable) if unreadable else None
        if messages:
            match await self._turns.offer(messages):
                case PromptAccepted():
                    logger.info("Matrix: handed %d message(s) to the session", len(messages))
                case PromptRejected(facts=ConversationFacts() as refusal):
                    recorded = refusal if recorded is None else recorded.then(*refusal.bodies)
                case PromptRejected():
                    logger.warning("Matrix: %d message(s) refused with no room to record it", len(messages))
        await self._store.advance(self._config.user_id, result.next_batch, recorded)

    async def _record_own_copy(
        self, projected: Sequence[ProjectedEvent], redactions: Sequence[Redaction], live_room: str | None
    ) -> None:
        """Keep what this batch showed of the room's own copy, and repair what it revealed.

        Filtered to the serviced room quietly — an own echo elsewhere is not an anomaly the way a
        stranger's message is, just not this console's copy. A duplicate the store reveals is the
        one failure the transaction id cannot close (a replay after Synapse's cache expired, before
        the first send's echo was recorded), and the room is owed a redaction for it.
        """
        showed = [event for event in projected if event.room_id == live_room]
        unsaid = [redaction for redaction in redactions if redaction.room_id == live_room]
        if not showed and not unsaid:
            return
        assert live_room is not None  # a room-scoped event's room equalled it
        for duplicate in await self._room_copy.record(showed, unsaid):
            await self._redact_duplicate(live_room, duplicate)

    async def _redact_duplicate(self, room_id: str, event_id: str) -> None:
        """Take back a second copy of a projected notice, best effort but never silent.

        Waited on so a success is real before the pass moves on; a failure is logged and released,
        because holding the watermark for it would wedge ingress on one cosmetic repair — the store
        keeps both live rows as the evidence, and the room keeps the duplicate until someone or the
        next observation of the pair redacts it.
        """
        logger.error("Matrix: %s shows a second copy of a projected notice; redacting %s", room_id, event_id)

        async def post() -> None:
            await self._client.redact(
                await self._token(), room_id, event_id, reason="duplicate projection of one conversation event"
            )

        try:
            await self.pacer.send_and_wait(post)
        except Exception:
            logger.exception("Matrix: could not redact duplicate %s; the room keeps both copies", event_id)

    async def _undelivered(self, messages: list[InboundMessage]) -> list[InboundMessage]:
        """The messages of a batch no prompt in the record carries yet.

        The rest are a re-delivery: the prompt they were folded into committed and the crash that
        followed lost only the acknowledgement. Offering them again would ask the same question
        twice — and would usually be refused, since the first copy is generally still queued, so
        the room would report a message as undelivered that the session was about to answer.
        """
        if not messages:
            return messages
        carried = await self._ledger.carried([message.event_id for message in messages])
        if carried:
            logger.info("Matrix: %d re-delivered event(s) the record already carries", len(carried))
        return [message for message in messages if message.event_id not in carried]

    async def _live_room(self, token: str, messages: Sequence[InboundMessage]) -> str | None:
        """The room being serviced, adopting one from traffic when nothing is bound.

        Membership already required an operator invite, so a room Haku is joined to and being
        spoken to in is one the operator put it in — adopting it recovers a binding rather than
        granting access. Without this, a room joined before the binding existed goes quiet forever
        with no way for the operator to revive it from a Matrix client.
        """
        if (binding := await self._conversations.bound_room()) is not None:
            return binding.room_id
        adopted = next((m.room_id for m in messages if m.sender == self._config.operator_user_id), None)
        if adopted is None:
            return None
        room = (await self._conversations.bind_room(adopted, await self._operator_id())).room_id
        logger.info("Matrix: adopted %s from traffic — no room was bound", room)
        self._queue_notice(room, "adopted this room — Haku had no room bound", RoomEventKind.ROOM)
        return room

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

        Unlike the OAuth refresh sweep, which takes the lock per pass, this holds it for the
        lifetime of the loop: `/sync` is a long poll, so releasing between passes would let two
        replicas interleave and double-process every batch.
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
        holding the sync lock — which is also why the budget it enforces is an estimate. The drain
        contends for a lock of its own, so it runs on one replica while the pacer runs on all of
        them, which is what keeps replies in order.
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
