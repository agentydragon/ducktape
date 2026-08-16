"""The console's Matrix sync loop.

Logs in as the bot, long-polls `/sync`, binds the one room Haku services (R3.6a), and
hands what the operator types to the session behind that room.

A batch the session cannot take yet is not held here: the watermark simply is not
advanced, so the homeserver re-delivers it next pass. Queue-until-turn-end (R2.2) and
"nothing is silently dropped" (R1.6) then need no local queue at all. A batch the session
*took* is not acknowledged here either, until the turn answering it has ended (R2.5) — the same
mechanism, extended over the one gap it did not cover, where a session that dies between the
enqueue and the turn leaves the prompt keyed to itself and the operator's message answered by
nobody (<../../../debug/message_drops.md> I3).

The one thing that mechanism cannot cover is an event Haku has no way to read — a screenshot,
a voice memo — because re-offering it would never converge on an answer. Those are announced in
the room and then acknowledged (`_report_unreadable`), which is the other half of R1.6.

It is also the only holder of a Matrix credential, so the session supervisor's lifecycle
notices go out through `announce` rather than through a second login — and so an answer, which
lives as a row until it has been said, is drained into the room from here (`outbox`).

The one thing it is asked *for* rather than told is this room's recent conversation
(`recent_history`), and that one is answered out of the console's own transcript. It is still
this object's to answer because it is the object that knows which room is bound; the credential
has nothing to do with it any more.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from uuid import UUID

from pydantic import SecretStr
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from haku.console.chat_models import ChatMessageRole, PromptFate
from haku.console.config import MatrixConfig
from haku.console.database_schema import MatrixHeldBatch, MatrixSyncState
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
from haku.console.x.channels.matrix.session import MatrixConversationStore, MatrixTurns, RoomTranscript
from haku.console.x.system_prompt import HistoryMessage

logger = logging.getLogger(__name__)

# Distinct from the OAuth refresh lock in oauth_association_maintenance.
_SYNC_ADVISORY_LOCK = 0x4D58_5359  # "MXSY"

# How long a replica that lost the election waits before trying again.
LEADER_RETRY = datetime.timedelta(seconds=30)
# Backoff after a failed sync, so a homeserver outage does not become a hot loop.
ERROR_BACKOFF = datetime.timedelta(seconds=10)
# Backoff after a pass that did not advance the watermark: a batch the session refused, or one
# whose turn is still running while newer messages wait behind it. Those events match the next
# `/sync` immediately — Synapse's long-poll only blocks waiting for *new* data, and to it nothing
# looks new — so without this a turn in flight while a message is waiting becomes a hot loop
# bounded only by round-trip time to the homeserver (observed: ~20/s). A batch that is merely
# waiting on its turn with nothing behind it does not need this: the poll runs from past that
# batch, so the homeserver has nothing to hand back and blocks as it should (`SyncPosition`).
UNADVANCED_BATCH_BACKOFF = datetime.timedelta(seconds=1)


@dataclass(frozen=True)
class HeldBatch:
    """A batch already with a session, whose acknowledgement is waiting on that session's turn."""

    next_batch: str
    message_id: UUID


@dataclass(frozen=True)
class SyncPosition:
    """Where the loop may say it has got to, and where it actually reads from.

    The two differ while a batch is held, and the distinction is R2.5: `watermark` is a promise —
    everything before it has been answered, and a crash resumes there — while `since` is only a
    cursor. Reading from the watermark instead would re-deliver, every pass, events a session
    already has, which is both re-offered work and a `/sync` that can never block (it is being
    asked for data the homeserver has already sent).
    """

    watermark: str | None
    held: HeldBatch | None

    @property
    def since(self) -> str | None:
        return self.watermark if self.held is None else self.held.next_batch


class MatrixSyncStore:
    """Durable sync state: the token we cached, the watermark we reached, and the batch we owe."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession]):
        self._sessions = sessions

    async def load(self, user_id: str) -> MatrixSyncState | None:
        async with self._sessions() as db:
            row: MatrixSyncState | None = await db.scalar(
                select(MatrixSyncState).where(MatrixSyncState.user_id == user_id)
            )
            return row

    async def save_token(self, user_id: str, token: str) -> None:
        async with self._sessions() as db, db.begin():
            if (row := await db.scalar(select(MatrixSyncState).where(MatrixSyncState.user_id == user_id))) is None:
                db.add(MatrixSyncState(user_id=user_id, access_token=token, next_batch=None))
            else:
                row.access_token = token

    async def position(self, user_id: str) -> SyncPosition:
        async with self._sessions() as db:
            state = await db.scalar(select(MatrixSyncState).where(MatrixSyncState.user_id == user_id))
            held = await db.scalar(select(MatrixHeldBatch).where(MatrixHeldBatch.user_id == user_id))
        return SyncPosition(
            watermark=state.next_batch if state is not None else None,
            held=None if held is None else HeldBatch(next_batch=held.next_batch, message_id=held.message_id),
        )

    async def save_batch(self, user_id: str, next_batch: str) -> None:
        """Advance the watermark.

        Written only for a batch that is finished with: the token is what makes an outage replay
        rather than skip (R1.7), so persisting it early loses messages. A batch that has been
        handed to a session is not finished with — that one goes through `hold`.
        """
        async with self._sessions() as db, db.begin():
            await self._advance(db, user_id, next_batch)

    async def hold(self, user_id: str, next_batch: str, message_id: UUID) -> None:
        """Withhold *next_batch* until the turn answering *message_id* has ended (R2.5)."""
        async with self._sessions() as db, db.begin():
            db.add(MatrixHeldBatch(user_id=user_id, next_batch=next_batch, message_id=message_id))

    async def acknowledge(self, user_id: str) -> None:
        """Publish the held batch's token as the watermark and stop holding it.

        One transaction, because the two halves are one statement: a watermark advanced without
        the row going too would hold the *next* batch against a turn that has already ended, and
        a row deleted without the watermark advancing would re-offer a batch already answered.
        """
        async with self._sessions() as db, db.begin():
            held = await db.scalar(select(MatrixHeldBatch).where(MatrixHeldBatch.user_id == user_id))
            if held is None:
                return
            await self._advance(db, user_id, held.next_batch)
            await db.delete(held)

    async def abandon(self, user_id: str) -> None:
        """Stop holding the batch without acknowledging it, so the next pass offers it again."""
        async with self._sessions() as db, db.begin():
            await db.execute(delete(MatrixHeldBatch).where(MatrixHeldBatch.user_id == user_id))

    @staticmethod
    async def _advance(db: AsyncSession, user_id: str, next_batch: str) -> None:
        row = await db.scalar(select(MatrixSyncState).where(MatrixSyncState.user_id == user_id))
        if row is None:
            db.add(MatrixSyncState(user_id=user_id, access_token=None, next_batch=next_batch))
        else:
            row.next_batch = next_batch


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
        self._holding = False
        self._status_event_id: str | None = None
        self._status_body: str | None = None

    async def _token(self) -> str:
        """A working access token, logging in only when the cached one is not.

        Synapse rate-limits `/login`, so re-authenticating on every pass would get the
        console throttled — hence the cache (R10.3a).
        """
        state = await self._store.load(self._config.user_id)
        if state is not None and state.access_token and await self._client.whoami(state.access_token):
            return state.access_token
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

        **The read still runs past the sync watermark, which is the property that had to survive.**
        The `/messages` read paginated back from the furthest position the loop had reached rather
        than from the watermark — the same token except while a batch is held, which is exactly
        when a replacement session starts, so a session replacing one that died mid-batch was not
        handed a history stopping short of the messages that killed it. Here that falls out instead
        of being arranged: ingress writes the prompt row when it **offers** a batch, and a batch is
        held precisely because it was offered, so a held batch is already in the transcript. The
        other half matches too — a batch that was *refused* has no row, and the token read did not
        show it either, since a refusal leaves the watermark behind it.

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

    async def sync_once(self, token: str) -> bool:
        """One `/sync` pass: act on what came back, and say whether the watermark moved.

        It moves only for a batch that is finished with. A batch the session cannot take yet
        leaves it where it is — the whole queue-until-turn-end mechanism (R2.2): the events stay
        unacknowledged on the homeserver and the next pass re-offers them, so nothing needs to be
        held here. A batch the session *did* take leaves it there too, until the turn answering it
        has ended (R2.5), which is what makes a session dying before that turn recoverable rather
        than a message acknowledged to nobody.

        The caller backs off on a False so re-offering does not become a hot loop against a
        homeserver that only long-polls for genuinely new data (`UNADVANCED_BATCH_BACKOFF`).
        """
        position = await self._store.position(self._config.user_id)
        result = await self._client.sync(token, position.since)
        for invite in result.invites:
            await self._handle_invite(token, invite)
        # Read after the invites, so a room bound by this very batch serves it too.
        live_room = await self._live_room(token, result.messages)
        # Resolved after the sync rather than before it, so a turn that ended while this pass was
        # long-polling is acknowledged and its successor offered in the same pass, instead of
        # costing the next message a whole backoff.
        if position.held is not None and not await self._resolve(position.held, live_room, result.messages):
            return False
        messages = self._serviced(result.messages, live_room)
        # `None` from a batch that was offered means the session cannot take it; `None` with no
        # batch to offer means there was nothing to hand over, and `messages` tells them apart.
        delivered = await self._turns.offer(messages) if messages else None
        if messages and delivered is None:
            self._report_holding(live_room, len(messages))
            return False
        if delivered is not None:
            self._holding = False
            logger.info("Matrix: handed %d message(s) to the session", len(messages))
        # After the offer, so a batch that was refused is not announced on every re-offer — an
        # unreadable event is announced on the pass its batch is taken, exactly once, because from
        # then on the loop reads from past it.
        self._report_unreadable(live_room, self._serviced(result.unmappable, live_room))
        if delivered is None:
            await self._store.save_batch(self._config.user_id, result.next_batch)
            return True
        await self._store.hold(self._config.user_id, result.next_batch, delivered)
        return False

    async def _resolve(self, held: HeldBatch, live_room: str | None, arrived: tuple[InboundMessage, ...]) -> bool:
        """Settle a batch already with a session; False while it is still owed an answer.

        The three fates are three things to do with the watermark, and `MatrixTurns.fate` is where
        the mapping is argued. `LOST` returns False having stopped holding, because the batch it
        needs to offer again is *behind* the cursor this pass read from — only a pass starting
        from the watermark will see it, which is the next one.
        """
        match await self._turns.fate(held.message_id):
            case PromptFate.IN_FLIGHT:
                self._report_holding(live_room, len(self._serviced(arrived, live_room)))
                return False
            case PromptFate.LOST:
                logger.warning(
                    "Matrix: the session holding prompt %s ended without answering it; offering the batch again",
                    held.message_id,
                )
                await self._store.abandon(self._config.user_id)
                return False
            case PromptFate.COMPLETED:
                await self._store.acknowledge(self._config.user_id)
                return True

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

    def _report_holding(self, live_room: str | None, count: int) -> None:
        """Tell the room once that its messages are waiting, not lost (R1.6).

        Once, not once per pass: a refused batch is re-offered on every sync until it is
        taken, and a long turn would otherwise fill the room with the same line.

        The reason is deliberately not named. A refusal means "not ready", which covers a
        turn in flight, a sandbox still provisioning, and no session at all — and the first
        message ever sent to a fresh room hits the last of those, where announcing "until
        the current turn finishes" describes a turn that does not exist.

        Nothing waiting is not a hold: a pass that is only waiting on a turn to end, with an empty
        room behind it, has nothing to report and would otherwise announce a hold on every turn.
        """
        if self._holding or live_room is None or not count:
            return
        self._holding = True
        self._queue_notice(live_room, f"holding {count} message(s) until Haku is ready", RoomEventKind.HOLDING)

    def _report_unreadable(self, live_room: str | None, events: list[UnmappableEvent]) -> None:
        """Say in the room that something arrived which Haku has no way to read (R1.6).

        **Surface and advance, rather than refuse the batch.** A refusal is only correct when it
        converges, and nothing about an `m.image` that has already been sent ever changes: the
        batch would be re-offered every pass forever, and one screenshot would wedge ingress
        against every message the operator sent afterwards — strictly worse than the drop this
        replaces. So the batch is acknowledged, and what is lost is an attachment that was never
        readable, said out loud in the room the operator sent it to.

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
                if not await self.sync_once(token):
                    await asyncio.sleep(UNADVANCED_BATCH_BACKOFF.total_seconds())
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
