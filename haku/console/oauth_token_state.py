"""Shared persistence and refresh state machine for Operator OAuth tokens."""

from __future__ import annotations

import asyncio
import datetime
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID, uuid4

from mcp.shared.auth import OAuthToken
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from tenacity import RetryCallState, Retrying, wait_exponential

from haku.console.database_schema import OAuthTokenState
from haku.console.oauth_token_support import token_expires_at, token_is_fresh
from haku.console.operator_identity_store import PostgresOperatorIdentityStore

logger = logging.getLogger(__name__)

type RefreshToken = Callable[[str], Awaitable[OAuthToken]]

_REFRESH_CLAIM_TTL = datetime.timedelta(seconds=30)
_REFRESH_CLAIM_POLL_SECONDS = 0.05
_REFRESH_RETRY_WAIT = wait_exponential(multiplier=30, max=datetime.timedelta(minutes=15))
_FAILURE_MESSAGE_LIMIT = 1024


class OAuthRefreshFailureKind(StrEnum):
    CONNECT = "connect"
    OUTCOME_UNKNOWN = "outcome_unknown"
    UPSTREAM = "upstream"
    OAUTH_REJECTED = "oauth_rejected"
    INVALID_RESPONSE = "invalid_response"
    INTERNAL = "internal"


class OAuthRefreshFailureAction(StrEnum):
    RETRYING = "retrying"
    RECONNECT = "reconnect"
    OPERATOR_ACTION = "operator_action"


class OAuthRefreshFailureDetail(BaseModel):
    at: datetime.datetime
    kind: OAuthRefreshFailureKind
    message: str


class OAuthRefreshFailureEpisode(BaseModel):
    model_config = ConfigDict(json_schema_serialization_defaults_required=True)

    started_at: datetime.datetime
    initial: OAuthRefreshFailureDetail
    latest: OAuthRefreshFailureDetail
    attempts: int
    resolution: str
    next_retry_at: datetime.datetime | None = None


class OAuthRefreshError(RuntimeError):
    """A sanitized, classified refresh failure safe to persist and reflect."""

    def __init__(self, message: str, *, kind: OAuthRefreshFailureKind, action: OAuthRefreshFailureAction) -> None:
        super().__init__(message)
        self.kind = kind
        self.action = action


class OAuthRefreshBlockedError(RuntimeError):
    """The durable failure state currently prevents another refresh attempt."""


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


def _refresh_retry_delay(failure_count: int) -> datetime.timedelta:
    """Backoff before the retry that follows the ``failure_count``-th consecutive failure.

    Tenacity is the wait calculator only: its strategies evaluate over a ``RetryCallState``, so a
    minimal state carrying the persisted failure count as its attempt number stands in for a live
    retry loop (the maintenance sweep drives actual retries off the stored ``refresh_retry_at``).
    """
    retry_state = RetryCallState(retry_object=Retrying(), fn=None, args=(), kwargs={})
    retry_state.attempt_number = failure_count
    return datetime.timedelta(seconds=_REFRESH_RETRY_WAIT(retry_state))


def new_oauth_token_state(
    *,
    operator_id: UUID,
    access_token: str,
    refresh_token: str | None,
    token_type: str,
    scope: str | None,
    expires_at: datetime.datetime | None,
    now: datetime.datetime,
) -> OAuthTokenState:
    return OAuthTokenState(
        operator_id=operator_id,
        token_revision=0,
        created_at=now,
        updated_at=now,
        access_token=access_token,
        refresh_token=refresh_token,
        token_type=token_type,
        scope=scope,
        token_expires_at=expires_at,
        refresh_claim_id=None,
        refresh_claim_expires_at=None,
        refresh_failure_count=0,
    )


def replace_oauth_token_state(
    state: OAuthTokenState,
    *,
    access_token: str,
    refresh_token: str | None,
    token_type: str,
    scope: str | None,
    expires_at: datetime.datetime | None,
    now: datetime.datetime,
) -> None:
    state.updated_at = now
    state.access_token = access_token
    state.refresh_token = refresh_token or state.refresh_token
    state.token_type = token_type
    state.scope = scope
    state.token_expires_at = expires_at
    state.token_revision += 1
    state.refresh_claim_id = None
    state.refresh_claim_expires_at = None
    state.refresh_failure_started_at = None
    state.refresh_failure_initial_kind = None
    state.refresh_failure_initial_message = None
    state.refresh_failure_latest_at = None
    state.refresh_failure_latest_kind = None
    state.refresh_failure_latest_message = None
    state.refresh_failure_count = 0
    state.refresh_failure_action = None
    state.refresh_retry_at = None


def refresh_failure_episode(state: OAuthTokenState) -> OAuthRefreshFailureEpisode | None:
    if state.refresh_failure_count == 0:
        return None
    assert state.refresh_failure_started_at is not None
    assert state.refresh_failure_initial_kind is not None
    assert state.refresh_failure_initial_message is not None
    assert state.refresh_failure_latest_at is not None
    assert state.refresh_failure_latest_kind is not None
    assert state.refresh_failure_latest_message is not None
    action = refresh_failure_action(state)
    assert action is not None
    return OAuthRefreshFailureEpisode(
        started_at=state.refresh_failure_started_at,
        initial=OAuthRefreshFailureDetail(
            at=state.refresh_failure_started_at,
            kind=OAuthRefreshFailureKind(state.refresh_failure_initial_kind),
            message=state.refresh_failure_initial_message,
        ),
        latest=OAuthRefreshFailureDetail(
            at=state.refresh_failure_latest_at,
            kind=OAuthRefreshFailureKind(state.refresh_failure_latest_kind),
            message=state.refresh_failure_latest_message,
        ),
        attempts=state.refresh_failure_count,
        resolution={
            OAuthRefreshFailureAction.RETRYING: "Retry scheduled automatically.",
            OAuthRefreshFailureAction.RECONNECT: "Reconnect the account before retrying.",
            OAuthRefreshFailureAction.OPERATOR_ACTION: "Operator action is required before retrying.",
        }[action],
        next_retry_at=state.refresh_retry_at,
    )


def refresh_failure_action(state: OAuthTokenState) -> OAuthRefreshFailureAction | None:
    if state.refresh_failure_action is None:
        return None
    return OAuthRefreshFailureAction(state.refresh_failure_action)


@dataclass(frozen=True, slots=True)
class _Fresh:
    access_token: str


@dataclass(frozen=True, slots=True)
class _Missing:
    pass


@dataclass(frozen=True, slots=True)
class _Wait:
    seconds: float


@dataclass(frozen=True, slots=True)
class _Claim:
    claim_id: UUID
    token_revision: int
    refresh_token: str


@dataclass(frozen=True, slots=True)
class _Blocked:
    failure: OAuthRefreshFailureEpisode


type _ClaimResult = _Fresh | _Missing | _Wait | _Claim | _Blocked


class PostgresOAuthTokenStateStore:
    """Own the single freshness/claim/refresh/write algorithm for every OAuth association."""

    def __init__(
        self, sessions: async_sessionmaker[AsyncSession], *, operator_identity_store: PostgresOperatorIdentityStore
    ) -> None:
        self._sessions = sessions
        self._operator_identity_store = operator_identity_store

    async def _claim(self, *, token_state_id: UUID, operator_id: UUID) -> _ClaimResult:
        async with self._sessions.begin() as session:
            await self._operator_identity_store.require_active_in_transaction(session, operator_id)
            state = await session.get(OAuthTokenState, token_state_id, with_for_update=True)
            if state is None or state.operator_id != operator_id:
                return _Missing()
            now = _now()
            if token_is_fresh(state.token_expires_at, now):
                return _Fresh(state.access_token)
            if not state.refresh_token:
                return _Missing()
            if (failure := refresh_failure_episode(state)) is not None:
                if refresh_failure_action(state) != OAuthRefreshFailureAction.RETRYING:
                    return _Blocked(failure)
                if failure.next_retry_at is not None and failure.next_retry_at > now:
                    return _Blocked(failure)
            if state.refresh_claim_expires_at is not None and state.refresh_claim_expires_at > now:
                return _Wait(min(_REFRESH_CLAIM_POLL_SECONDS, (state.refresh_claim_expires_at - now).total_seconds()))
            claim_id = uuid4()
            state.refresh_claim_id = claim_id
            state.refresh_claim_expires_at = now + _REFRESH_CLAIM_TTL
            return _Claim(claim_id=claim_id, token_revision=state.token_revision, refresh_token=state.refresh_token)

    async def _release_claim(self, *, token_state_id: UUID, claim_id: UUID) -> None:
        async with self._sessions.begin() as session:
            state = await session.get(OAuthTokenState, token_state_id, with_for_update=True)
            if state is not None and state.refresh_claim_id == claim_id:
                state.refresh_claim_id = None
                state.refresh_claim_expires_at = None

    async def _store_failure(self, *, token_state_id: UUID, claim_id: UUID, error: Exception) -> None:
        async with self._sessions.begin() as session:
            state = await session.get(OAuthTokenState, token_state_id, with_for_update=True)
            if state is None or state.refresh_claim_id != claim_id:
                return
            now = _now()
            if isinstance(error, OAuthRefreshError):
                kind = error.kind
                action = error.action
                message = str(error).strip()[:_FAILURE_MESSAGE_LIMIT] or type(error).__name__
            else:
                kind = OAuthRefreshFailureKind.INTERNAL
                action = OAuthRefreshFailureAction.RETRYING
                message = f"OAuth token refresh failed: {type(error).__name__}"
            if state.refresh_failure_count == 0:
                state.refresh_failure_started_at = now
                state.refresh_failure_initial_kind = kind
                state.refresh_failure_initial_message = message
            state.refresh_failure_latest_at = now
            state.refresh_failure_latest_kind = kind
            state.refresh_failure_latest_message = message
            state.refresh_failure_count += 1
            state.refresh_failure_action = action
            if action == OAuthRefreshFailureAction.RETRYING:
                state.refresh_retry_at = now + _refresh_retry_delay(state.refresh_failure_count)
            else:
                state.refresh_retry_at = None
            state.refresh_claim_id = None
            state.refresh_claim_expires_at = None

    async def _store_refreshed(
        self, *, token_state_id: UUID, operator_id: UUID, claim: _Claim, refreshed: OAuthToken
    ) -> str | None:
        now = _now()
        async with self._sessions.begin() as session:
            await self._operator_identity_store.require_active_in_transaction(session, operator_id)
            state = await session.get(OAuthTokenState, token_state_id, with_for_update=True)
            if state is None or state.operator_id != operator_id:
                return None
            if (
                state.refresh_claim_id != claim.claim_id
                or state.token_revision != claim.token_revision
                or state.refresh_token != claim.refresh_token
            ):
                if token_is_fresh(state.token_expires_at, now):
                    return state.access_token
                raise RuntimeError("OAuth token state changed during refresh; retry the tool call")
            replace_oauth_token_state(
                state,
                access_token=refreshed.access_token,
                refresh_token=refreshed.refresh_token,
                token_type=refreshed.token_type,
                scope=refreshed.scope or state.scope,
                expires_at=token_expires_at(refreshed, now),
                now=now,
            )
            return state.access_token

    async def access_token_for(self, *, token_state_id: UUID, operator_id: UUID, refresh: RefreshToken) -> str | None:
        while True:
            match await self._claim(token_state_id=token_state_id, operator_id=operator_id):
                case _Fresh(access_token):
                    return access_token
                case _Missing():
                    return None
                case _Wait(seconds):
                    await asyncio.sleep(seconds)
                case _Blocked(failure):
                    raise OAuthRefreshBlockedError(failure.latest.message)
                case _Claim() as claim:
                    break
        try:
            refreshed = await refresh(claim.refresh_token)
            return await self._store_refreshed(
                token_state_id=token_state_id, operator_id=operator_id, claim=claim, refreshed=refreshed
            )
        except Exception as error:
            await self._store_failure(token_state_id=token_state_id, claim_id=claim.claim_id, error=error)
            raise
        except BaseException:
            try:
                await self._release_claim(token_state_id=token_state_id, claim_id=claim.claim_id)
            except Exception:
                logger.exception("Failed to release OAuth refresh claim for token state %s", token_state_id)
            raise
