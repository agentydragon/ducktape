"""The console's Matrix sync loop.

Logs in as the bot, long-polls `/sync`, binds the one room Haku services (R3.6a), and
hands what the operator types to the session behind that room.

A batch the session cannot take yet is not held here: the watermark simply is not
advanced, so the homeserver re-delivers it next pass. Queue-until-turn-end (R2.2) and
"nothing is silently dropped" (R1.6) then need no local queue at all.

The one thing that mechanism cannot cover is an event Haku has no way to read — a screenshot,
a voice memo — because re-offering it would never converge on an answer. Those are announced in
the room and then acknowledged (`_report_unreadable`), which is the other half of R1.6.

It is also the only holder of a Matrix credential, so the session supervisor's lifecycle
notices go out through `announce` rather than through a second login.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import UUID

from pydantic import SecretStr
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from haku.console.config import MatrixConfig
from haku.console.database_schema import MatrixSyncState
from haku.console.x.matrix_client import (
    EventTag,
    InboundMessage,
    Invite,
    MatrixAuthError,
    MatrixClient,
    RoomEventKind,
    UnmappableEvent,
)
from haku.console.x.matrix_pacer import RoomPacer
from haku.console.x.matrix_session import MatrixConversationStore, MatrixTurns

logger = logging.getLogger(__name__)

# Distinct from the OAuth refresh lock in oauth_association_maintenance.
_SYNC_ADVISORY_LOCK = 0x4D58_5359  # "MXSY"

# How long a replica that lost the election waits before trying again.
LEADER_RETRY = datetime.timedelta(seconds=30)
# Backoff after a failed sync, so a homeserver outage does not become a hot loop.
ERROR_BACKOFF = datetime.timedelta(seconds=10)
# Backoff after a refused batch. `sync_once` deliberately does not advance the watermark on
# refusal (R2.2 — Matrix itself holds the message), but that means the same still-unread events
# match the next `/sync` immediately: Synapse's long-poll only blocks waiting for *new* data,
# and to it nothing looks new. Without this, a turn in flight while a message is waiting turns
# into a hot loop bounded only by round-trip time to the homeserver (observed: ~20/s).
REFUSED_BATCH_BACKOFF = datetime.timedelta(seconds=1)


class MatrixSyncStore:
    """Durable sync state: the access token we cached and the watermark we reached."""

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

    async def save_batch(self, user_id: str, next_batch: str) -> None:
        """Advance the watermark.

        Written only after the batch's events have been acted on: the token is what makes
        an outage replay rather than skip (R1.7), so persisting it early loses messages.
        """
        async with self._sessions() as db, db.begin():
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
    ):
        # Taken separately from `config`, which carries it as optional: the service is
        # only ever constructed once the password is known to be there (R10.3b).
        self._config = config
        self._password = password
        self._engine = engine
        self._store = store
        self._conversations = conversations
        self._turns = turns
        self._client = MatrixClient(config.homeserver, config.user_id, config.device_id)
        # Public because it has a lifecycle, and this object's owner is the one that drives it:
        # everything the console says into the room goes through here, so it outlives no
        # individual send and belongs to whoever is running the service.
        self.pacer = RoomPacer()
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

    async def reply(self, body: str, tag: EventTag) -> None:
        """Post Haku's answer into the live room as ordinary text (R11.1).

        `tag` is what makes the event say which transcript row it is — and, through
        `EventTag.transaction_id`, what stops the same row being posted twice: this is the one
        send whose identity the homeserver can hold us to.
        """
        conversation = await self._conversations.load(self._config.user_id)
        if conversation is None:
            logger.error("Matrix: a turn finished with no room bound; dropping the answer")
            return
        room_id = conversation.room_id

        async def post() -> None:
            await self._client.send_text(await self._token(), room_id, body, txn_id=tag.transaction_id(), tag=tag)

        self.pacer.send(post)

    async def show_status(self, body: str, session_id: UUID | None = None) -> None:
        """Make the room's single status line say *body*, creating or editing it (R6.2, R6.5).

        One line per turn rather than a notice per step: a room where every tool call is a
        message is a room nobody reads. The event id of the live line is held here because
        this is the only object that knows the room and the token; the turn loop says what
        the state is and never learns how it is shown.

        **Idempotent, and paced by the room rather than by this call.** The floor moved to the
        caller (`_TurnStatus`), because deciding what the line should say and deciding when it
        may change have to be one decision; the room's own budget is `matrix_pacer`'s, where
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

    async def recent_history(self, limit: int) -> tuple[InboundMessage, ...]:
        """The tail of the live room's conversation, for re-awakening a replacement session.

        Empty before the loop has ever synced: with no watermark there is no pagination token
        to read back from, and a room nothing has synced has nothing in it that Haku said.
        """
        conversation = await self._conversations.load(self._config.user_id)
        state = await self._store.load(self._config.user_id)
        if conversation is None or state is None or state.next_batch is None:
            return ()
        return await self._client.recent_messages(
            await self._token(), conversation.room_id, since=state.next_batch, limit=limit
        )

    async def announce(self, body: str, kind: RoomEventKind = RoomEventKind.LIFECYCLE) -> None:
        """Post a lifecycle notice into the live room, if there is one.

        The supervisor's outbound path (`matrix_session.Announce`): it owns session
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
        """One `/sync` pass: act on what came back, then advance the watermark.

        Returns False, without advancing, when the session cannot take the batch. That is the
        whole queue-until-turn-end mechanism (R2.2): the events stay unacknowledged on the
        homeserver and the next pass re-offers them, so nothing needs to be held here. The
        caller backs off on a False so re-offering does not become a hot loop against a
        homeserver that only long-polls for genuinely new data (`REFUSED_BATCH_BACKOFF`).
        """
        state = await self._store.load(self._config.user_id)
        result = await self._client.sync(token, state.next_batch if state else None)
        for invite in result.invites:
            await self._handle_invite(token, invite)
        # Read after the invites, so a room bound by this very batch serves it too.
        live_room = await self._live_room(token, result.messages)
        if messages := self._serviced(result.messages, live_room):
            if not await self._turns.offer(messages):
                self._report_holding(live_room, len(messages))
                return False
            self._holding = False
            logger.info("Matrix: handed %d message(s) to the session", len(messages))
        # After the offer, so a batch that was refused is not announced on every re-offer; and on
        # this path only, which is the one that advances the watermark — so each unreadable event
        # is announced exactly once, on the pass that acknowledges it.
        self._report_unreadable(live_room, self._serviced(result.unmappable, live_room))
        await self._store.save_batch(self._config.user_id, result.next_batch)
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
        """
        if self._holding or live_room is None:
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
                    await asyncio.sleep(REFUSED_BATCH_BACKOFF.total_seconds())
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
        """Hold the sync loop and the room's outbound queue for as long as this replica runs.

        The pacer runs on **every** replica, not only the sync leader: the turn loop speaks
        through this object from whichever replica holds the session's lease, which is not
        generally the one holding the sync lock. That is also why the budget it enforces is an
        estimate — see `matrix_pacer`.
        """
        task = asyncio.create_task(self._run(), name="matrix-sync")
        try:
            async with self.pacer.run():
                yield
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            await self._client.close()
