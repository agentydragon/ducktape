from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime
from pathlib import Path

from mautrix.types import RoomAlias, RoomID
from nio import AsyncClient, AsyncClientConfig, RoomMessageText
from nio.events.invite_events import InviteMemberEvent
from nio.events.room_events import MegolmEvent
from nio.exceptions import LocalProtocolError
from nio.responses import (
    InviteInfo,
    JoinedRoomsError,
    JoinedRoomsResponse,
    JoinError,
    KeysClaimError,
    KeysQueryError,
    KeysUploadError,
    RoomResolveAliasError,
    RoomResolveAliasResponse,
    SyncError,
    SyncResponse,
    WhoamiError,
    WhoamiResponse,
)
from pydantic import BaseModel, ConfigDict

from ember.config import MatrixSettings
from ember.secrets import ProjectedSecret

logger = logging.getLogger(__name__)


class ConversationStatus(BaseModel):
    last_user_message_at: datetime | None = None
    last_agent_message_at: datetime | None = None
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class MatrixClient:
    """Matrix client that streams new room messages via an async queue."""

    def __init__(self, settings: MatrixSettings, debounce_seconds: float = 0.5) -> None:
        self._settings = settings
        self._client: AsyncClient | None = None
        self._since: str | None = None
        self._state_store = settings.state_store
        self._user_id: str | None = None
        self._sync_task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()
        self._queue: asyncio.Queue[RoomMessageText] = asyncio.Queue()
        self._debounce = max(debounce_seconds, 0.1)
        self._control_rooms: set[RoomID] = set()
        self._token_secret = settings.access_token_secret
        self._store_dir = settings.store_dir
        self._device_id = settings.device_id
        self._pickle_key = settings.pickle_key
        self._status_lock = asyncio.Lock()
        self._room_status: dict[RoomID, ConversationStatus] = {}
        self._aggregate_status = ConversationStatus()

    @classmethod
    def from_projected_secrets(
        cls,
        *,
        base_url: str | None = None,
        access_token_secret: ProjectedSecret | None = None,
        admin_user_id: str | None = None,
        state_store: Path | None = None,
        store_dir: Path | None = None,
        device_id: str = "ember-device",
        pickle_key: str = "ember-matrix-store",
    ) -> MatrixClient:
        resolved_base_url = base_url or os.environ.get("MATRIX_BASE_URL")
        if not resolved_base_url:
            raise RuntimeError("MATRIX_BASE_URL must be set (e.g. https://matrix.example.org)")

        secret = access_token_secret or ProjectedSecret(name="matrix_access_token", env_var="MATRIX_ACCESS_TOKEN")

        state_path = Path(state_store or "/var/lib/ember/matrix_state.json").expanduser()
        store_path = Path(store_dir or "/var/lib/ember/matrix_store").expanduser()
        settings = MatrixSettings(
            base_url=resolved_base_url,
            access_token_secret=secret,
            admin_user_id=admin_user_id,
            state_store=state_path,
            store_dir=store_path,
            device_id=device_id,
            pickle_key=pickle_key,
        )
        return cls(settings)

    @property
    def configured(self) -> bool:
        return bool(self._settings.base_url and self._token_secret.value())

    async def start(self) -> None:
        if not self.configured:
            raise RuntimeError("Matrix client is not configured correctly")
        if self._sync_task and not self._sync_task.done():
            return

        logger.info("Starting Matrix client for homeserver %s", self._settings.base_url)

        self._since = None
        self._queue = asyncio.Queue()
        self._room_status = {}
        self._aggregate_status = ConversationStatus()
        self._client = await self._create_client()
        self._control_rooms = await self._initialise_control_rooms()
        self._since = self._load_since_token()

        rooms_display = ", ".join(sorted(str(room) for room in self._control_rooms)) or "(none)"
        logger.info("Initialized Matrix client; tracking %d joined rooms: %s", len(self._control_rooms), rooms_display)

        self._stop_event.clear()
        self._sync_task = asyncio.create_task(self._sync_loop(), name="matrix-sync-loop")

    async def stop(self) -> None:
        self._stop_event.set()
        if self._sync_task is not None:
            self._sync_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._sync_task
            self._sync_task = None
        if self._client is not None:
            await self._client.close()
            self._client = None

    async def close(self) -> None:
        await self.stop()

    async def __aenter__(self) -> MatrixClient:
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()

    @asynccontextmanager
    async def session(self) -> AsyncIterator[MatrixClient]:
        """Context manager for Matrix client session. Returns self for direct usage."""
        await self.start()
        try:
            yield self
        finally:
            await self.close()

    async def get_events(self) -> list[RoomMessageText]:
        """Wait for a batch of new events (debounced). Use asyncio.timeout() around this call to add a timeout."""
        first = await self._queue.get()

        events = [first]
        # Debounce to accumulate events that land together
        await asyncio.sleep(self._debounce)
        while not self._queue.empty():
            events.append(self._queue.get_nowait())
        return events

    async def send_text_message(self, room_id: RoomID | str, body: str, *, msgtype: str = "m.notice") -> None:
        """Send a text message to a Matrix room."""
        if self._client is None:  # pragma: no cover - defensive
            raise RuntimeError("Matrix client is not running; call start() first")

        resolved_room = await self._ensure_room_id(room_id)
        await self._client.room_send(
            room_id=str(resolved_room), message_type="m.room.message", content={"msgtype": msgtype, "body": body}
        )
        logger.info("Sent Matrix message to %s", resolved_room)

    @property
    def client(self) -> AsyncClient:
        """Access the underlying nio AsyncClient. For advanced usage only."""
        if self._client is None:  # pragma: no cover - defensive
            raise RuntimeError("Matrix client not initialised")
        return self._client

    async def _initialise_control_rooms(self) -> set[RoomID]:
        return await self._fetch_joined_rooms()

    async def _fetch_joined_rooms(self) -> set[RoomID]:
        try:
            response = await self.client.joined_rooms()
        except Exception as exc:
            logger.warning("Failed to fetch joined rooms: %s", exc)
            return set()

        if isinstance(response, JoinedRoomsError):
            logger.warning("Matrix joined_rooms error: %s", response.message)
            return set()
        if not isinstance(response, JoinedRoomsResponse):
            logger.warning("Unexpected joined_rooms response: %r", response)
            return set()
        return {RoomID(room_id) for room_id in (response.rooms or [])}

    async def _sync_loop(self) -> None:
        try:
            while not self._stop_event.is_set():
                if (response := await self._sync_once()) is None:
                    await asyncio.sleep(5)
                    continue

                await self._process_sync_with_client(response)

                logger.debug(
                    "Matrix sync succeeded; next_batch=%s, joined=%d, invite=%d, leave=%d",
                    response.next_batch,
                    len(response.rooms.join or {}),
                    len(response.rooms.invite or {}),
                    len(response.rooms.leave or {}),
                )

                self._since = response.next_batch
                self._persist_since_token(response.next_batch)
                await self._handle_invites(response)
                await self._handle_joined_rooms(response)
                await self._handle_left_rooms(response)
                await self._crypto_housekeeping()
        except asyncio.CancelledError:
            raise

    async def _handle_invites(self, response: SyncResponse) -> None:
        if not (invites := response.rooms.invite):
            return

        for room_id_value, invite in invites.items():
            room_id = RoomID(room_id_value)
            if await self._should_accept_invite(room_id, invite):
                await self._accept_invite(room_id)

    async def _handle_joined_rooms(self, response: SyncResponse) -> None:
        joined = response.rooms.join or {}
        for room_id_value, room in joined.items():
            room_id = RoomID(room_id_value)
            timeline = room.timeline.events or []
            logger.debug(
                "Matrix room %s timeline has %d events (limited=%s)",
                room_id,
                len(timeline),
                room.timeline.limited if room.timeline else None,
            )
            last_event_id: str | None = None
            for event in timeline:
                if isinstance(event, RoomMessageText):
                    await self._queue.put(event)
                    last_event_id = event.event_id
                    body = event.body
                    if not isinstance(body, str):
                        body = str(body)
                    logger.info("[matrix] room=%s sender=%s event=%s: %s", room_id, event.sender, event.event_id, body)
                    await self._record_message_event(room_id, event)
                elif isinstance(event, MegolmEvent):
                    try:
                        sender = event.sender
                    except AttributeError:
                        sender = "unknown"
                    logger.debug(
                        "Matrix room %s awaiting room keys for event %s from %s", room_id, event.event_id, sender
                    )
                else:
                    try:
                        sender = event.sender
                    except AttributeError:
                        sender = "unknown"
                    logger.debug("Matrix room %s ignoring event type=%s from %s", room_id, type(event).__name__, sender)

            if last_event_id is not None:
                await self._mark_read(room_id, last_event_id)

    async def _record_message_event(self, room_id: RoomID, event: RoomMessageText) -> None:
        timestamp = datetime.fromtimestamp(event.server_timestamp / 1000.0, tz=UTC)
        async with self._status_lock:
            tracker = self._room_status.setdefault(room_id, ConversationStatus())
            if self._user_id is not None and event.sender == self._user_id:
                tracker.last_agent_message_at = timestamp
            else:
                tracker.last_user_message_at = timestamp

            self._aggregate_status = ConversationStatus(
                last_user_message_at=_latest_timestamp(
                    status.last_user_message_at for status in self._room_status.values()
                ),
                last_agent_message_at=_latest_timestamp(
                    status.last_agent_message_at for status in self._room_status.values()
                ),
            )

    async def _handle_left_rooms(self, response: SyncResponse) -> None:
        if not (left := response.rooms.leave):
            return
        left_ids = {RoomID(room_id) for room_id in left}
        removed = left_ids & self._control_rooms
        if not removed:
            return
        self._control_rooms.difference_update(removed)
        for room_id in removed:
            logger.info("Removed control room %s after leave", room_id)

    async def get_conversation_status(self) -> ConversationStatus:
        async with self._status_lock:
            return self._aggregate_status.model_copy()

    async def set_typing(self, room_ids: Iterable[RoomID | str], typing: bool, timeout_ms: int = 30000) -> None:
        """Send typing notifications for the given rooms."""

        if self._client is None:
            raise RuntimeError("Matrix client not initialised")

        for room_id in room_ids:
            try:
                rid = await self._ensure_room_id(room_id)
                await self._client.room_typing(str(rid), typing_state=typing, timeout=timeout_ms if typing else 0)
                logger.debug("Sent typing=%s notification for room %s (timeout=%sms)", typing, rid, timeout_ms)
            except Exception as exc:
                logger.warning("Failed to send typing notification for %s: %s", room_id, exc)

    async def _should_accept_invite(self, room_id: RoomID, invite: InviteInfo) -> bool:
        admin_user = self._settings.admin_user_id
        if admin_user is None:
            logger.debug("Skipping invite to %s; MATRIX_ADMIN_USER_ID not set", room_id)
            return False

        if self._user_id is None:
            logger.debug("Skipping invite to %s; user ID not established", room_id)
            return False

        events = invite.invite_state or []
        for event in events:
            if (
                isinstance(event, InviteMemberEvent)
                and event.sender == admin_user
                and event.state_key == self._user_id
                and event.membership == "invite"
            ):
                logger.info("Accepting admin invite to %s", room_id)
                return True
        logger.debug("Invite to %s ignored; no admin invite event found", room_id)
        return False

    async def _accept_invite(self, room_id: RoomID) -> None:
        try:
            response = await self.client.join(str(room_id))
        except Exception as exc:
            logger.warning("Failed to join invited room %s: %s", room_id, exc)
            return

        if isinstance(response, JoinError):
            logger.warning("Matrix join error for %s: %s", room_id, response.message)
            return

        self._control_rooms.add(room_id)
        logger.info("Joined control room %s", room_id)

    async def _mark_read(self, room_id: RoomID, event_id: str) -> None:
        if self._client is None:
            raise RuntimeError("Matrix client not initialised")
        try:
            await self._client.room_read_markers(str(room_id), fully_read_event=event_id, read_event=event_id)
        except Exception as exc:
            logger.warning("Failed to update Matrix read marker for %s: %s", room_id, exc)

    async def _ensure_room_id(self, room: RoomID | str) -> RoomID:
        identifier = str(room)
        if identifier.startswith("!"):
            return RoomID(identifier)
        if self._client is None:
            raise RuntimeError("Matrix client not initialised")
        alias = RoomAlias(identifier)
        response = await self._client.room_resolve_alias(str(alias))
        if isinstance(response, RoomResolveAliasError):
            raise RuntimeError(f"Failed to resolve Matrix alias {alias}: {response.message}")
        if not isinstance(response, RoomResolveAliasResponse) or not response.room_id:
            raise RuntimeError(f"Matrix alias {alias} returned no room_id")
        return RoomID(response.room_id)

    async def _create_client(self) -> AsyncClient:
        base_url = self._settings.base_url
        if base_url is None:
            raise RuntimeError("Matrix base URL and access token must be configured")

        homeserver = base_url.rstrip("/")
        self._store_dir.mkdir(parents=True, exist_ok=True)
        client = AsyncClient(
            homeserver=homeserver,
            user="",
            device_id=self._device_id,
            store_path=str(self._store_dir),
            config=self._client_config(),
        )
        client.access_token = self._read_access_token()

        whoami = await client.whoami()
        if isinstance(whoami, WhoamiError):
            raise RuntimeError(f"Matrix whoami failed: {whoami.message}")
        if not isinstance(whoami, WhoamiResponse) or not whoami.user_id:
            raise RuntimeError("Matrix whoami response missing user_id")

        self._user_id = whoami.user_id
        client.user_id = whoami.user_id
        await self._bootstrap_crypto(client)
        return client

    async def _sync_once(self) -> SyncResponse | None:
        if self._client is None:
            return None
        self._apply_access_token()
        try:
            response = await self._client.sync(timeout=30_000, since=self._since)
        except Exception as exc:
            logger.exception("Matrix sync failed: %s", exc)
            return None

        if isinstance(response, SyncError):
            logger.error("Matrix sync error: %s", response.message)
            return None

        if not isinstance(response, SyncResponse):
            logger.error("Unexpected Matrix sync response: %r", response)
            return None

        return response

    async def _process_sync_with_client(self, response: SyncResponse) -> None:
        if self._client is None:
            return
        try:
            await self._client.receive_response(response)
        except Exception as exc:
            logger.warning("Matrix client failed to process sync response: %s", exc)

    def _read_access_token(self) -> str:
        token = self._token_secret.value()
        if token is None:
            raise RuntimeError("Matrix access token is not configured")
        return token

    def _apply_access_token(self) -> None:
        if self._client is None:
            return
        self._client.access_token = self._read_access_token()

    def _load_since_token(self) -> str | None:
        try:
            data = self._state_store.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except OSError as exc:
            logger.warning("Failed to read Matrix state store %s: %s", self._state_store, exc)
            return None

        token = data.strip() or None
        if token:
            logger.info("Loaded Matrix sync token from %s", self._state_store)
        return token

    def _persist_since_token(self, token: str) -> None:
        try:
            self._state_store.parent.mkdir(parents=True, exist_ok=True)
            self._state_store.write_text(token, encoding="utf-8")
        except OSError as exc:
            logger.warning("Failed to persist Matrix sync token to %s: %s", self._state_store, exc)

    def _client_config(self) -> AsyncClientConfig:
        return AsyncClientConfig(encryption_enabled=True, pickle_key=self._pickle_key, store_sync_tokens=True)

    async def _bootstrap_crypto(self, client: AsyncClient) -> None:
        if not client.config.encryption_enabled:
            return
        try:
            client.load_store()
        except LocalProtocolError as exc:
            logger.warning("Unable to load Matrix crypto store: %s", exc)
            return
        await self._service_crypto_tasks(client)

    async def _crypto_housekeeping(self) -> None:
        client = self._client
        if client is None or not client.config.encryption_enabled:
            return
        await self._service_crypto_tasks(client)

    async def _service_crypto_tasks(self, client: AsyncClient) -> None:
        try:
            if client.should_upload_keys:
                upload = await client.keys_upload()
                if isinstance(upload, KeysUploadError):
                    logger.warning("Matrix key upload failed: %s", upload.message)
            if client.should_query_keys:
                query = await client.keys_query()
                if isinstance(query, KeysQueryError):
                    logger.warning("Matrix key query failed: %s", query.message)
            if client.should_claim_keys:
                targets = client.get_users_for_key_claiming()
                if targets:
                    claim = await client.keys_claim(targets)
                    if isinstance(claim, KeysClaimError):
                        logger.warning("Matrix key claim failed: %s", claim.message)
        except LocalProtocolError as exc:
            logger.debug("Matrix crypto maintenance skipped: %s", exc)


def _latest_timestamp(values: Iterable[datetime | None]) -> datetime | None:
    return max((v for v in values if v is not None), default=None)
