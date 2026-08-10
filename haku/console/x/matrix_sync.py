"""The console's Matrix sync loop.

Logs in as the bot, long-polls `/sync`, binds the one room Haku services (R3.6a), and
acts on what the operator types. Still echoing rather than running a turn — Phase 1 of
`haku/plans/matrix_chat_runtime.md` replaces `_handle_message`; the credential, the loop,
the watermark and the send path are already proven under it.

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
from uuid import uuid4

from pydantic import SecretStr
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from haku.console.config import MatrixConfig
from haku.console.database_schema import MatrixSyncState
from haku.console.x.matrix_client import InboundMessage, Invite, MatrixAuthError, MatrixClient
from haku.console.x.matrix_session import MatrixConversationStore

logger = logging.getLogger(__name__)

# Distinct from the OAuth refresh lock in oauth_association_maintenance.
_SYNC_ADVISORY_LOCK = 0x4D58_5359  # "MXSY"

# How long a replica that lost the election waits before trying again.
LEADER_RETRY = datetime.timedelta(seconds=30)
# Backoff after a failed sync, so a homeserver outage does not become a hot loop.
ERROR_BACKOFF = datetime.timedelta(seconds=10)


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
    ):
        # Taken separately from `config`, which carries it as optional: the service is
        # only ever constructed once the password is known to be there (R10.3b).
        self._config = config
        self._password = password
        self._engine = engine
        self._store = store
        self._conversations = conversations
        self._client = MatrixClient(config.homeserver, config.user_id, config.device_id)
        self._sent_event_ids: set[str] = set()

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
            await self._send_notice(token, live_room, f"invited to another room; still serving this one ({live_room})")
            return
        await self._client.join(token, invite.room_id)
        logger.info("Matrix: joined %s on invite from %s", invite.room_id, invite.inviter)
        await self._send_notice(token, invite.room_id, "joined — this is now Haku's room")

    async def announce(self, body: str) -> None:
        """Post a lifecycle notice into the live room, if there is one.

        The supervisor's outbound path (`matrix_session.Announce`): it owns session
        lifecycle but never a Matrix credential, so it speaks through the loop that has one.
        A no-op before any room is bound — there is genuinely nowhere to say it.
        """
        conversation = await self._conversations.load(self._config.user_id)
        if conversation is None:
            logger.info("Matrix: no room bound yet, dropping notice: %s", body)
            return
        await self._send_notice(await self._token(), conversation.room_id, body)

    async def _send_notice(self, token: str, room_id: str, body: str) -> None:
        # A fresh transaction ID rather than a derived one: Synapse deduplicates per access
        # token, the token outlives a restart, and any counter we could derive would reset —
        # so a notice after a restart would be silently swallowed as a replay of an older
        # one. Notices have no retry to make idempotent, so there is nothing to trade away.
        event_id = await self._client.send_notice(token, room_id, body, txn_id=uuid4().hex)
        self._sent_event_ids.add(event_id)

    async def _handle_message(self, token: str, message: InboundMessage, live_room: str | None) -> None:
        if message.event_id in self._sent_event_ids:
            return
        if message.room_id != live_room:
            # Only the bound room is serviced (R3.6a). Reachable for a room joined before
            # the binding existed, and for anything that gets Haku into a room by a path
            # other than an invite.
            logger.warning("Matrix: ignoring %s from unserviced room %s", message.event_id, message.room_id)
            return
        event_id = await self._client.send_text(
            token, message.room_id, f"echo: {message.body}", txn_id=f"echo-{message.event_id}"
        )
        self._sent_event_ids.add(event_id)
        logger.info("Matrix: echoed %s from %s", message.event_id, message.sender)

    async def sync_once(self, token: str) -> None:
        """One `/sync` pass: act on what came back, then advance the watermark."""
        state = await self._store.load(self._config.user_id)
        result = await self._client.sync(token, state.next_batch if state else None)
        for invite in result.invites:
            await self._handle_invite(token, invite)
        # Read after the invites, so a room bound by this very batch serves it too.
        conversation = await self._conversations.load(self._config.user_id)
        live_room = conversation.room_id if conversation is not None else None
        for message in result.messages:
            await self._handle_message(token, message, live_room)
        await self._store.save_batch(self._config.user_id, result.next_batch)

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
        task = asyncio.create_task(self._run(), name="matrix-sync")
        try:
            yield
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            await self._client.close()
