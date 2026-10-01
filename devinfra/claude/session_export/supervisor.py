"""Owns the OAuth credential and runs the sync loop for as long as one exists (design: docs/serve.md).

One process holds the credential, since a refresh rotates the refresh token. Pairing therefore happens here:
the page asks for an attempt, the human approves in a browser and pastes back the URL it was sent to, and the
credential this saves replaces whatever the loop was using.
"""

import asyncio
import contextlib
import logging
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Self

import httpx
from pydantic import BaseModel, Field

from devinfra.claude.session_export.api import SessionsApi
from devinfra.claude.session_export.failures import describe_failure
from devinfra.claude.session_export.live import LiveFollower, LiveProblem
from devinfra.claude.session_export.oauth import (
    CALLBACK_PORT,
    DEFAULT_SCOPES,
    CredentialStore,
    OAuthTokenSource,
    PairingAttempt,
    redeem,
)
from devinfra.claude.session_export.settings import ControlSettings, ServeSettings
from devinfra.claude.session_export.store import SessionStore
from devinfra.claude.session_export.sync import sync_once

logger = logging.getLogger(__name__)


class SyncState(StrEnum):
    UNPAIRED = "unpaired"
    SYNCING = "syncing"
    IDLE = "idle"


class CredentialStatus(BaseModel):
    organization_uuid: str
    scopes: list[str]
    access_token_expires_at: datetime = Field(description="Refreshed automatically shortly before it lapses.")


class CycleStatus(BaseModel):
    finished_at: datetime
    behind: int = Field(description="Sessions that had moved on when the cycle began.")
    events_read: int


class FailureStatus(BaseModel):
    at: datetime
    message: str


class LiveStatus(BaseModel):
    following: bool = Field(description="Live following is on and has a credential to run with.")
    watching: bool = Field(description="A session watch is open, or being reopened.")
    streams: int = Field(description="Sessions with an event stream open, or being reopened.")
    last_event_at: datetime | None = Field(description="When an event last arrived over a stream.")
    problems: list[LiveProblem] = Field(description="What is failing now and retrying; empty when all is well.")
    failure: FailureStatus | None = Field(description="Why following stopped; the polling cycle carries on.")


class SyncStatus(BaseModel):
    state: SyncState
    pairing_started: bool = Field(description="A pairing attempt is waiting for the redirect URL to be pasted back.")
    credential: CredentialStatus | None
    sessions: int
    sessions_behind: int = Field(
        description="Sessions the store is behind on that no live stream covers: the next poll reads them."
    )
    poll_interval_seconds: float = Field(description="The pause between one polling cycle and the next.")
    last_cycle: CycleStatus | None
    last_failure: FailureStatus | None = Field(description="The latest cycle's error; cleared by the next success.")
    live: LiveStatus


class SyncSupervisor:
    def __init__(
        self,
        *,
        credentials: CredentialStore,
        store: SessionStore,
        token_client: httpx.AsyncClient,
        interval: float,
        workers: int,
        live_streams: int,
        live_window: timedelta,
        scopes: Sequence[str] = DEFAULT_SCOPES,
        api_transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        """`live_streams`: how many sessions to follow by event stream at once; 0 leaves the sync to its cycles."""
        self._credentials = credentials
        self._store = store
        self._token_client = token_client
        self._interval = interval
        self._workers = workers
        self._live_streams = live_streams
        self._live_window = live_window
        self._scopes = scopes
        self._api_transport = api_transport
        self._state = SyncState.IDLE
        self._pending: PairingAttempt | None = None
        # Bumped by every pairing: the loop ends its run on the credential it started with.
        self._credential_version = 0
        self._wake = asyncio.Event()
        self._last_cycle: CycleStatus | None = None
        self._last_failure: FailureStatus | None = None
        self._follower: LiveFollower | None = None
        self._live_failure: FailureStatus | None = None

    @classmethod
    def for_settings(
        cls, settings: ControlSettings | ServeSettings, *, store: SessionStore, token_client: httpx.AsyncClient
    ) -> Self:
        return cls(
            credentials=CredentialStore(settings.credentials_file),
            store=store,
            token_client=token_client,
            interval=settings.interval_seconds,
            workers=settings.workers,
            live_streams=settings.live_streams,
            live_window=timedelta(seconds=settings.live_window_seconds),
        )

    def start_pairing(self) -> str:
        """The URL to open in a browser signed in to the account. Replaces an attempt still waiting."""
        self._pending = PairingAttempt.start(scopes=self._scopes, port=CALLBACK_PORT)
        return self._pending.url

    async def finish_pairing(self, redirect_url: str) -> None:
        """Redeem the pasted URL, save the credential and have the loop switch to it.

        Raises `ValueError` for a URL that is not the waiting attempt's approval, and `httpx.HTTPStatusError`
        when Anthropic refuses the code. The attempt is spent either way: a code is single-use.
        """
        attempt, self._pending = self._pending, None
        if attempt is None:
            raise ValueError("no pairing attempt is waiting: start one first")
        credential = await redeem(self._token_client, attempt, attempt.code_from_redirect(redirect_url))
        self._credentials.save(credential)
        logger.info(
            "paired organization=%s scopes=%s", credential.organization_uuid, " ".join(sorted(credential.scopes))
        )
        self._credential_version += 1
        self._wake.set()

    def sync_now(self) -> None:
        self._wake.set()

    async def status(self) -> SyncStatus:
        credential = self._credentials.load() if self._credentials.exists() else None
        counts = await self._store.counts(followed=self._follower.followed if self._follower else frozenset())
        return SyncStatus(
            state=self._state if credential else SyncState.UNPAIRED,
            pairing_started=self._pending is not None,
            credential=(
                CredentialStatus(
                    organization_uuid=credential.organization_uuid,
                    scopes=sorted(credential.scopes),
                    access_token_expires_at=credential.expires_at,
                )
                if credential
                else None
            ),
            sessions=counts.sessions,
            sessions_behind=counts.behind,
            poll_interval_seconds=self._interval,
            last_cycle=self._last_cycle,
            last_failure=self._last_failure,
            live=LiveStatus(
                following=self._follower is not None,
                watching=self._follower is not None and self._follower.watching,
                streams=self._follower.streams if self._follower else 0,
                last_event_at=self._follower.last_event_at if self._follower else None,
                problems=self._follower.problems if self._follower else [],
                failure=self._live_failure,
            ),
        )

    async def run(self) -> None:
        while True:
            version = self._credential_version
            if not self._credentials.exists():
                await self._until_paired(version)
                continue
            source = OAuthTokenSource(self._credentials, self._token_client)
            async with SessionsApi.for_oauth(source, transport=self._api_transport) as api:
                await self._cycles(api, version)

    async def _until_paired(self, version: int) -> None:
        while self._credential_version == version:
            await self._wake.wait()
            self._wake.clear()

    async def _cycles(self, api: SessionsApi, version: int) -> None:
        """Poll on the credential `version`, and follow live alongside when that is on."""
        if not self._live_streams:
            await self._poll(api, version, follower=None)
            return
        follower = LiveFollower(api, self._store, max_streams=self._live_streams, window=self._live_window)
        self._follower, self._live_failure = follower, None
        try:
            async with asyncio.TaskGroup() as tasks:
                following = tasks.create_task(self._follow(follower))
                await self._poll(api, version, follower=follower)
                following.cancel()
        finally:
            self._follower = None

    async def _follow(self, follower: LiveFollower) -> None:
        try:
            await follower.run()
        except Exception as failure:  # the polling cycle is the safety net, so this must not end the process
            logger.exception("live following stopped")
            self._live_failure = FailureStatus(at=datetime.now(UTC), message=describe_failure(failure))

    async def _poll(self, api: SessionsApi, version: int, *, follower: LiveFollower | None) -> None:
        while self._credential_version == version:
            self._state = SyncState.SYNCING
            try:
                result = await sync_once(api, self._store, workers=self._workers)
            except Exception as failure:  # shown on the page; the next cycle retries
                logger.exception("sync cycle failed")
                self._last_failure = FailureStatus(at=datetime.now(UTC), message=describe_failure(failure))
            else:
                self._last_failure = None
                self._last_cycle = CycleStatus(
                    finished_at=datetime.now(UTC), behind=result.behind, events_read=result.events_read
                )
            self._state = SyncState.IDLE
            if follower:
                follower.refresh()
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout(self._interval):
                    await self._wake.wait()
            self._wake.clear()
