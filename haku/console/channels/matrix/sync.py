"""The console's Matrix sync loop.

Logs in as the bot, long-polls `/sync` — one owner for the user-wide token — and dispatches what
came back by room: each room the operator has invited Haku into is bound to a conversation of its
own, and a batch's messages are offered to the conversation their room is attached to.

**Every pass acknowledges what it read.** A batch the session will not take is rejected rather than
held: the operator is told so and sends it again, so nothing queues behind a running turn and the
loop keeps one position instead of two. An event Haku has no way to read — a screenshot, a voice
memo — is the same shape, because re-offering one could never converge.

Both are **recorded in the transaction that advances the watermark**, as `conversation_event` rows
the room notice is a rendering of. Advancing first and announcing afterwards would let one crash
lose the message and the notice together. The watermark is the user's, so one transaction carries
every room's facts of the pass.

An accepted batch is the one thing that commits *before* the watermark, so a crash in between
re-delivers it. That is what `ingress_ledger` is for: the loop asks the record which events a
prompt already carries rather than trusting its own position.

The same `/sync` also carries Haku's own events back. Ingress drops them; the mirror reader keeps
them (`room_copy`) — recorded ahead of the watermark, never treated as input — and a second live
copy of one projected notice is redacted here, where the credential is.

It is also the only holder of a Matrix credential, and the sync leader is where everything that
speaks into a room runs: the channel's own room-binding notices go out through `_queue_notice`
rather than a second login, and each live attachment's reconciler
(`attachment_reconciler.AttachmentReconcilers`, swept per pass under this loop's election) says
what its room is owed from the record — sealed notices, span lines and queued answers — through
this object's frontend methods and its per-attachment send budgets (`RoomPacers`).
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime
import logging
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from uuid import UUID, uuid4

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from haku.console.channels.matrix.attachment_reconciler import AttachmentReconciler, AttachmentReconcilers
from haku.console.channels.matrix.client import (
    AuthError,
    Client,
    ConversationEventSource,
    EventTag,
    InboundMessage,
    Invite,
    ProjectedEvent,
    Redaction,
    RoomEventKind,
    UnmappableEvent,
)
from haku.console.channels.matrix.config import Config
from haku.console.channels.matrix.conversation import (
    ConversationFacts,
    ConversationStore,
    PromptAccepted,
    PromptRejected,
    RoomAttachment,
    Turns,
)
from haku.console.channels.matrix.conversation_subscriber import ConversationSubscriber
from haku.console.channels.matrix.ingress_ledger import IngressLedger
from haku.console.channels.matrix.outbox import PendingReply, RoomOutbox, RoomOutboxDrain
from haku.console.channels.matrix.outbox_wake import OutboxWakes
from haku.console.channels.matrix.pacer import RoomPacers
from haku.console.channels.matrix.revisions import Revision, RevisionLog
from haku.console.channels.matrix.room_copy import RoomCopy
from haku.console.channels.matrix.spans import Span, SpanKind
from haku.console.database_schema import MatrixAccessToken, MatrixSyncWatermark
from haku.console.operator_identity_store import PostgresOperatorIdentityStore
from haku.console.x.conversation_log import writer_for
from haku.console.x.conversation_wakes import ConversationWakes
from haku.console.x.subscription import ConversationStream

logger = logging.getLogger(__name__)

# Distinct from the OAuth refresh lock in oauth/association_maintenance.py.
_SYNC_ADVISORY_LOCK = 0x4D58_5359  # "MXSY"

# How long a replica that lost the election waits before trying again.
LEADER_RETRY = datetime.timedelta(seconds=30)
# Backoff after a failed sync, so a homeserver outage does not become a hot loop.
ERROR_BACKOFF = datetime.timedelta(seconds=10)


class SyncStore:
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

        Upserted rather than read-then-inserted: several queued sends can log in concurrently, and
        a leadership change can put two replicas' sends in flight at once, so two writers can
        first-write this row together; the loser of a read-then-insert fails on the primary key,
        which on the sender's side is a queued send lost.
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

    async def advance(self, user_id: str, next_batch: str, facts: Sequence[ConversationFacts] = ()) -> None:
        """Acknowledge everything up to *next_batch*, recording what the pass decided about it.

        **One transaction.** `facts` are what this pass decided that exists nowhere else — a prompt
        a conversation rejected, an attachment it could not read — one entry per conversation the
        pass decided something about, each the durable half of a notice its room is about to be
        told. Committing the watermark first would let a crash acknowledge a message to the
        homeserver while losing both the record of it and the operator's only account of what
        happened to it.

        The watermark is what makes an outage replay rather than skip, so what is written here must
        be a position genuinely finished with.
        """
        now = datetime.datetime.now(datetime.UTC)
        async with self._sessions() as db, db.begin():
            for recorded in facts:
                if not recorded.bodies:
                    continue
                writer = await writer_for(
                    db, recorded.conversation_id, session_id=recorded.session_id, turn_id=None, now=now
                )
                for body in recorded.bodies:
                    writer.authored(body)
            await db.execute(
                insert(MatrixSyncWatermark)
                .values(user_id=user_id, next_batch=next_batch)
                .on_conflict_do_update(index_elements=["user_id"], set_={"next_batch": next_batch})
            )


class SyncService:
    """Runs `/sync` on whichever replica holds the advisory lock, and hosts its rooms' reconcilers."""

    def __init__(
        self,
        config: Config,
        engine: AsyncEngine,
        store: SyncStore,
        conversations: ConversationStore,
        identities: PostgresOperatorIdentityStore,
        turns: Turns,
        outbox: RoomOutbox,
        revisions: RevisionLog,
        ledger: IngressLedger,
        room_copy: RoomCopy,
        outbox_wakes: OutboxWakes,
        sessions: async_sessionmaker[AsyncSession],
        stream: ConversationStream,
        notifications: ConversationWakes,
    ):
        self._config = config
        self._password = config.password
        self._engine = engine
        self._store = store
        self._conversations = conversations
        self._identities = identities
        self._turns = turns
        self._revisions = revisions
        self._ledger = ledger
        self._room_copy = room_copy
        self._sessions = sessions
        self._stream = stream
        self._notifications = notifications
        self._room_outbox = outbox
        self._outbox_wakes = outbox_wakes
        self._client = Client(config.homeserver, config.user_id, config.device_id)
        # Public because its lifecycle is the owner's to drive: everything the console says into
        # a room goes through its attachment's queue, so the registry outlives every send.
        self.pacers = RoomPacers()
        # Held here rather than by the composition root: each reconciler needs the credential that
        # can speak into its room and the budget that decides when, which only this object has.
        self._reconcilers = AttachmentReconcilers(self._reconciler_for)

    async def _reconciler_for(self, binding: RoomAttachment) -> AttachmentReconciler:
        subscriber = ConversationSubscriber(
            self._sessions,
            self._stream,
            self._notifications,
            self.project_notice,
            self,
            binding,
            self._room_outbox,
            self._room_copy,
        )
        drain = RoomOutboxDrain(
            self._room_outbox,
            await self.pacers.for_attachment(binding.attachment_id),
            self.post_reply,
            binding,
            self._outbox_wakes,
        )
        return AttachmentReconciler(binding, subscriber, drain)

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
        """Join invites from the operator; each invited room binds a conversation of its own."""
        if invite.inviter != self._config.operator_user_id:
            logger.warning(
                "Matrix: leaving invite to %s from %s pending — not the operator", invite.room_id, invite.inviter
            )
            return
        # Binding opens the room's conversation, so this needs the Operator it belongs to. An
        # identity that cannot be resolved yet raises, leaving the invite unjoined; the homeserver
        # keeps reporting it, so the next pass tries again.
        binding = await self._conversations.bind_room(invite.room_id, await self._operator_id())
        await self._client.join(token, invite.room_id)
        logger.info("Matrix: joined %s on invite from %s", invite.room_id, invite.inviter)
        await self._queue_notice(binding, "joined — this is now Haku's room", RoomEventKind.ROOM)

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

    def _span_tag(self, attachment_id: UUID, span: Span) -> EventTag:
        """The tag every event of a span carries: its kind, and the opening event as the source.

        The source is what lets `room_copy` hold the editable copy's correspondence — the create,
        every edit and the seal all name the same durable position — and what derives the create's
        deterministic Matrix transaction id.
        """
        kind = RoomEventKind.STATUS if span.kind is SpanKind.TURN else RoomEventKind.LIFECYCLE
        return EventTag(
            kind=kind,
            source=ConversationEventSource(
                attachment_id=attachment_id, conversation_id=span.conversation_id, event_seq=span.opened_seq
            ),
        )

    async def show_span(self, room_id: str, attachment_id: UUID, span: Span, body: str) -> None:
        """Make one span's line say *body*, creating or editing it.

        One line per span rather than a notice per step: a room where every tool call or setup line
        is a message is a room nobody reads. The edit floor belongs to the caller
        (`spans.LiveSpans.reconcile`), because what the line should say and when it may change are
        one decision; the room's budget is `pacer`'s, where a span is the one sender allowed to
        overwrite what it has not yet said.

        Create-or-edit is decided inside the queued send, because the create is what produces the
        event id the edit needs and may not have happened at queue time. The pacer is serial, so by
        the time an edit runs its create has.

        **Which event to edit comes from `matrix_revision`, not from this process.** The line
        outlives the replica that posted it: an adopting replica edits the line its predecessor
        posted instead of posting a second one beside it — and the create's transaction id is
        derived from the span's source, so even a create replayed before its revision row committed
        is refused by the homeserver rather than doubled.
        """
        tag = self._span_tag(attachment_id, span)

        async def post() -> None:
            token = await self._token()
            if (showing := await self._revisions.live(attachment_id, span.subject)) is None:
                event_id = await self._client.send_notice(token, room_id, body, txn_id=tag.transaction_id(), tag=tag)
                await self._revisions.record(attachment_id, span.subject, event_id)
                return
            # A fresh transaction id per edit: each edit is its own event, and a lost one is
            # recomputed by the level-triggered reconciler rather than replayed.
            await self._client.edit_notice(token, room_id, showing.event_id, body, txn_id=uuid4().hex, tag=tag)

        (await self.pacers.for_attachment(attachment_id)).revise(span.subject, post)

    async def seal_span(self, room_id: str, attachment_id: UUID, span: Span, body: str) -> None:
        """Close one span's line with its final words, keeping it in scrollback.

        Unlike `show_span`, this does not return while the effect exists only in the pacer's
        memory: the subscriber advances its cursor after this returns, so a send failure or process
        death leaves the closing event owed and the replay repeats the seal.

        Three arms, decided inside the queued send: a live line is edited to the final body and its
        revision retired (a replayed seal re-edits the same content, harmlessly); a span whose
        source the room already shows with no live revision was sealed before the crash — or the
        operator redacted the line, which is respected either way; and a span that never had a line
        posts one, under the source-derived transaction id, which is exactly the sealed one-event
        notice this generalises.
        """
        tag = self._span_tag(attachment_id, span)
        pacer = await self.pacers.for_attachment(attachment_id)
        pacer.drop(span.subject)

        async def post() -> None:
            token = await self._token()
            if (showing := await self._revisions.live(attachment_id, span.subject)) is not None:
                await self._client.edit_notice(token, room_id, showing.event_id, body, txn_id=uuid4().hex, tag=tag)
                await self._revisions.retire(showing.revision_id)
                return
            if await self._room_copy.shows(attachment_id, span.opened_seq):
                return
            await self._client.send_notice(token, room_id, body, txn_id=tag.transaction_id(), tag=tag)

        await pacer.send_and_wait(post)

    async def retire_span(self, room_id: str, attachment_id: UUID, span: Span) -> None:
        """Withdraw one span's line, if it was ever created.

        Called when a span's live state is spent — a status line left saying "running Bash" after
        the turn died is the stuck-typing-indicator bug in another costume.

        A change still waiting to go out is dropped rather than sent and then redacted, which would
        spend two of the room's ten sends showing something for a fraction of a second. Best effort
        past that: a redact lost with its replica is repaired by the takeover sweep.
        """
        pacer = await self.pacers.for_attachment(attachment_id)
        pacer.drop(span.subject)

        async def retire() -> None:
            if (showing := await self._revisions.live(attachment_id, span.subject)) is None:
                return
            await self._client.redact(await self._token(), room_id, showing.event_id, reason="live state spent")
            await self._revisions.retire(showing.revision_id)

        pacer.send(retire)

    async def retire_stale_spans(self, room_id: str, attachment_id: UUID, keep: frozenset[str]) -> None:
        """Redact every live revision no open span accounts for.

        The takeover repair: a retirement that died with its replica, and lines under subjects this
        release no longer writes — the pre-span singleton `"status"` included — are all the same
        stale line to an operator. `keep` names the subjects the fold still owns.
        """
        pacer = await self.pacers.for_attachment(attachment_id)
        for stale in await self._revisions.live_all(attachment_id):
            if stale.subject in keep:
                continue
            revision = stale.revision

            async def retire(revision: Revision = revision) -> None:
                await self._client.redact(await self._token(), room_id, revision.event_id, reason="stale span line")
                await self._revisions.retire(revision.revision_id)

            pacer.send(retire)

    async def set_typing(self, room_id: str, active: bool) -> None:
        """Show or hide Haku's typing indicator in *room_id*.

        Best effort by construction: a failed typing notice is cosmetic, where a turn that died
        because the room could not be told it was thinking would not be. The homeserver expires the
        notice on its own, so a lost `False` is a stale indicator for seconds, not forever.
        """
        try:
            await self._client.set_typing(await self._token(), room_id, active=active)
        except Exception:
            logger.warning("Matrix: typing notification failed (active=%s)", active, exc_info=True)

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

        Unlike `_queue_notice`, this call does not return while the effect exists only in the
        pacer's memory. `ConversationSubscriber` advances its cursor after this returns, so a send
        failure or process death leaves the source event owed. Replaying it uses the same Matrix
        transaction id.
        """
        tag = EventTag(
            kind=kind,
            source=ConversationEventSource(
                attachment_id=attachment_id, conversation_id=conversation_id, event_seq=source_event_seq
            ),
        )

        async def post() -> None:
            await self._client.send_notice(await self._token(), room_id, body, txn_id=tag.transaction_id(), tag=tag)

        await (await self.pacers.for_attachment(attachment_id)).send_and_wait(post)

    async def _queue_notice(self, binding: RoomAttachment, body: str, kind: RoomEventKind) -> None:
        tag = EventTag(kind=kind)

        async def post() -> None:
            await self._client.send_notice(
                await self._token(), binding.room_id, body, txn_id=tag.transaction_id(), tag=tag
            )

        (await self.pacers.for_attachment(binding.attachment_id)).send(post)

    async def sync_once(self, token: str) -> None:
        """One `/sync` pass: act on what came back, room by room, and acknowledge all of it.

        The token is user-wide, so one batch carries every room's events; the pass dispatches them
        by room and offers each serviced room's messages to the conversation its attachment names.
        The watermark always moves, because everything this pass read has been answered one way or
        the other — handed to a conversation, or rejected and said so — and what every room's slice
        decided is written with the watermark rather than after it.

        **What was recorded, the rooms are not told here.** The row is the notice: each
        attachment's `ConversationSubscriber` renders it from the record at its own position, so
        this pass writes and stops. Every refusal reaches a row — what a rejection is about is the
        conversation, which exists as soon as the room is bound — and a room bound to nothing has
        nowhere to record one, which is also why it is not serviced.

        **A re-delivered message is dropped from the batch rather than offered again**, and what
        makes that safe is that the ledger only knows an event because a prompt in the record
        carries it.

        **The rooms' own copies are recorded before the watermark moves.** Recording is idempotent,
        so a crash in between re-records rather than forgets — which is what lets a reconciler
        treat "the watermark is past an echo" as "its correspondence is durable".
        """
        result = await self._client.sync(token, await self._store.watermark(self._config.user_id))
        for invite in result.invites:
            await self._handle_invite(token, invite)
        # Read after the invites, so a room bound by this very batch serves it too.
        bindings = await self._serviced_rooms(result.messages)
        await self._record_own_copy(result.projected, result.redactions, bindings)
        inbound: list[InboundMessage | UnmappableEvent] = [*result.messages, *result.unmappable]
        facts: list[ConversationFacts] = []
        for room_id in dict.fromkeys(event.room_id for event in inbound):
            events = [event for event in inbound if event.room_id == room_id]
            if bindings.get(room_id) is None:
                # Only rooms holding a conversation are serviced. Reachable for a joined room whose
                # traffic is not the operator's, and for anything that gets Haku into a room
                # without an invite.
                for event in events:
                    logger.warning("Matrix: ignoring %s from unserviced room %s", event.event_id, room_id)
                continue
            recorded = await self._service_room(
                bindings[room_id],
                [event for event in events if isinstance(event, InboundMessage)],
                [event for event in events if isinstance(event, UnmappableEvent)],
            )
            if recorded is not None:
                facts.append(recorded)
        await self._store.advance(self._config.user_id, result.next_batch, facts)

    async def _service_room(
        self, binding: RoomAttachment, messages: list[InboundMessage], unreadable: list[UnmappableEvent]
    ) -> ConversationFacts | None:
        """Offer one room's slice of the batch to its conversation; the facts are the caller's to
        append with the watermark."""
        messages = await self._undelivered(messages)
        recorded = await self._turns.unreadable(binding, unreadable) if unreadable else None
        if messages:
            match await self._turns.offer(binding, messages):
                case PromptAccepted():
                    logger.info(
                        "Matrix: handed %d message(s) to the conversation behind %s", len(messages), binding.room_id
                    )
                case PromptRejected(facts=refusal):
                    recorded = refusal if recorded is None else recorded.then(*refusal.bodies)
        return recorded

    async def _record_own_copy(
        self,
        projected: Sequence[ProjectedEvent],
        redactions: Sequence[Redaction],
        bindings: Mapping[str, RoomAttachment],
    ) -> None:
        """Keep what this batch showed of the rooms' own copies, and repair what it revealed.

        Filtered to the serviced rooms quietly — an own echo elsewhere is not an anomaly the way a
        stranger's message is, just not this console's copy. A duplicate the store reveals is the
        one failure the transaction id cannot close (a replay after Synapse's cache expired, before
        the first send's echo was recorded), and its room is owed a redaction for it — found
        through the copy's own attachment, since the duplicate need not be an event of this batch.
        """
        showed = [event for event in projected if event.room_id in bindings]
        unsaid = [redaction for redaction in redactions if redaction.room_id in bindings]
        if not showed and not unsaid:
            return
        by_attachment = {binding.attachment_id: binding for binding in bindings.values()}
        for duplicate in await self._room_copy.record(showed, unsaid):
            if (binding := by_attachment.get(duplicate.attachment_id)) is None:
                logger.warning(
                    "Matrix: duplicate %s is under attachment %s, which is no longer live; leaving it",
                    duplicate.event_id,
                    duplicate.attachment_id,
                )
                continue
            await self._redact_duplicate(binding, duplicate.event_id)

    async def _redact_duplicate(self, binding: RoomAttachment, event_id: str) -> None:
        """Take back a second copy of a projected notice, best effort but never silent.

        Waited on so a success is real before the pass moves on; a failure is logged and released,
        because holding the watermark for it would wedge ingress on one cosmetic repair — the store
        keeps both live rows as the evidence, and the room keeps the duplicate until someone or the
        next observation of the pair redacts it.
        """
        logger.error("Matrix: %s shows a second copy of a projected notice; redacting %s", binding.room_id, event_id)

        async def post() -> None:
            await self._client.redact(
                await self._token(), binding.room_id, event_id, reason="duplicate projection of one conversation event"
            )

        try:
            await (await self.pacers.for_attachment(binding.attachment_id)).send_and_wait(post)
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

    async def _serviced_rooms(self, messages: Sequence[InboundMessage]) -> dict[str, RoomAttachment]:
        """The live bindings by room, adopting any unbound room the operator is speaking in.

        Membership already required an operator invite, so a room Haku is joined to and being
        spoken to in is one the operator put it in — adopting it recovers a binding rather than
        granting access. Without this, a room joined before its binding existed goes quiet forever
        with no way for the operator to revive it from a Matrix client.
        """
        bindings = {binding.room_id: binding for binding in await self._conversations.live_attachments()}
        for room_id in dict.fromkeys(
            message.room_id for message in messages if message.sender == self._config.operator_user_id
        ):
            if room_id in bindings:
                continue
            binding = await self._conversations.bind_room(room_id, await self._operator_id())
            bindings[binding.room_id] = binding
            logger.info("Matrix: adopted %s from traffic — no conversation was bound to it", room_id)
            await self._queue_notice(binding, "adopted this room — no conversation was bound to it", RoomEventKind.ROOM)
        return bindings

    async def _run_as_leader(self) -> None:
        """Sync until cancelled. Only ever entered holding the advisory lock.

        The reconciler sweep runs ahead of each pass, so a binding one pass creates — an invite,
        an adoption, a database edit — is served from the next, which begins as soon as this
        pass's batch is acknowledged.
        """
        token = await self._token()
        while True:
            try:
                await self._reconcilers.sweep(await self._conversations.live_attachments())
                await self.sync_once(token)
            except AuthError:
                logger.warning("Matrix: access token rejected, logging in again")
                token = await self._token()
            except Exception:
                logger.exception("Matrix: sync pass failed")
                await asyncio.sleep(ERROR_BACKOFF.total_seconds())

    async def _run(self) -> None:
        """Contend for leadership, and sync for as long as we hold it.

        Unlike the OAuth refresh sweep, which takes the lock per pass, this holds it for the
        lifetime of the loop: `/sync` is a long poll, so releasing between passes would let two
        replicas interleave and double-process every batch. The attachments' reconcilers — and the
        outbox wake wire their drains share — live inside the held-lock block, which is what makes
        each of them singular cluster-wide without an election of its own; losing leadership stops
        them all, and the next leader's sweep starts its own.
        """
        while True:
            async with self._engine.connect() as leader:
                if not await leader.scalar(text("SELECT pg_try_advisory_lock(:lock)"), {"lock": _SYNC_ADVISORY_LOCK}):
                    await asyncio.sleep(LEADER_RETRY.total_seconds())
                    continue
                logger.info("Matrix: this replica is the sync leader")
                try:
                    await self._outbox_wakes.start()
                    await self._run_as_leader()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("Matrix: sync loop exited, retrying")
                    await asyncio.sleep(ERROR_BACKOFF.total_seconds())
                finally:
                    with contextlib.suppress(Exception):
                        await self._reconcilers.aclose()
                    with contextlib.suppress(Exception):
                        await self._outbox_wakes.aclose()
                    with contextlib.suppress(Exception):
                        await leader.scalar(text("SELECT pg_advisory_unlock(:lock)"), {"lock": _SYNC_ADVISORY_LOCK})

    @asynccontextmanager
    async def run(self) -> AsyncIterator[None]:
        """Hold the sync loop and the per-attachment send queues.

        Everything that speaks into a room runs on the sync leader — this loop's own binding
        narration and duplicate repair, and each attachment's reconciler — so only the leader
        populates the queues; a replica that loses the election holds an empty registry. The
        queues still outlive the leader task here, so a shutdown flushes what the reconcilers
        managed to queue.
        """
        async with self.pacers.run():
            task = asyncio.create_task(self._run(), name="matrix-sync")
            try:
                yield
            finally:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        await self._client.close()
