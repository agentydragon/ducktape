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
from pydantic import BaseModel
from sqlalchemy.orm import Session, sessionmaker

from haku.console.database_schema import OAuthTokenState
from haku.console.oauth_token_support import token_expires_at, token_is_fresh
from haku.console.operator_identity_store import PostgresOperatorIdentityStore

logger = logging.getLogger(__name__)

type RefreshToken = Callable[[str], Awaitable[OAuthToken]]

_REFRESH_CLAIM_TTL = datetime.timedelta(seconds=30)
_REFRESH_CLAIM_POLL_SECONDS = 0.05
_REFRESH_RETRY_BASE = datetime.timedelta(seconds=30)
_REFRESH_RETRY_MAX = datetime.timedelta(minutes=15)
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
    started_at: datetime.datetime
    initial: OAuthRefreshFailureDetail
    latest: OAuthRefreshFailureDetail
    attempts: int
    action: OAuthRefreshFailureAction
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
    assert state.refresh_failure_action is not None
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
        action=OAuthRefreshFailureAction(state.refresh_failure_action),
        next_retry_at=state.refresh_retry_at,
    )


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
        self, sessions: sessionmaker[Session], *, operator_identity_store: PostgresOperatorIdentityStore
    ) -> None:
        self._sessions = sessions
        self._operator_identity_store = operator_identity_store

    def _claim(self, *, token_state_id: UUID, operator_id: UUID) -> _ClaimResult:
        with self._sessions.begin() as session:
            self._operator_identity_store.require_active_in_transaction(session, operator_id)
            state = session.get(OAuthTokenState, token_state_id, with_for_update=True)
            if state is None or state.operator_id != operator_id:
                return _Missing()
            now = _now()
            if token_is_fresh(state.token_expires_at, now):
                return _Fresh(state.access_token)
            if not state.refresh_token:
                return _Missing()
            if (failure := refresh_failure_episode(state)) is not None:
                if failure.action != OAuthRefreshFailureAction.RETRYING:
                    return _Blocked(failure)
                if failure.next_retry_at is not None and failure.next_retry_at > now:
                    return _Blocked(failure)
            if state.refresh_claim_expires_at is not None and state.refresh_claim_expires_at > now:
                return _Wait(min(_REFRESH_CLAIM_POLL_SECONDS, (state.refresh_claim_expires_at - now).total_seconds()))
            claim_id = uuid4()
            state.refresh_claim_id = claim_id
            state.refresh_claim_expires_at = now + _REFRESH_CLAIM_TTL
            return _Claim(claim_id=claim_id, token_revision=state.token_revision, refresh_token=state.refresh_token)

    def _release_claim(self, *, token_state_id: UUID, claim_id: UUID) -> None:
        with self._sessions.begin() as session:
            state = session.get(OAuthTokenState, token_state_id, with_for_update=True)
            if state is not None and state.refresh_claim_id == claim_id:
                state.refresh_claim_id = None
                state.refresh_claim_expires_at = None

    def _store_failure(self, *, token_state_id: UUID, claim_id: UUID, error: Exception) -> None:
        with self._sessions.begin() as session:
            state = session.get(OAuthTokenState, token_state_id, with_for_update=True)
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
                # Unknown exceptions can contain request bodies, DSNs, or other secrets. The full
                # traceback remains in the caller's logs; persist only its safe class identity.
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
                delay_seconds = min(
                    _REFRESH_RETRY_BASE.total_seconds() * 2 ** (state.refresh_failure_count - 1),
                    _REFRESH_RETRY_MAX.total_seconds(),
                )
                state.refresh_retry_at = now + datetime.timedelta(seconds=delay_seconds)
            else:
                state.refresh_retry_at = None
            state.refresh_claim_id = None
            state.refresh_claim_expires_at = None

    def _store_refreshed(
        self, *, token_state_id: UUID, operator_id: UUID, claim: _Claim, refreshed: OAuthToken
    ) -> str | None:
        now = _now()
        with self._sessions.begin() as session:
            self._operator_identity_store.require_active_in_transaction(session, operator_id)
            state = session.get(OAuthTokenState, token_state_id, with_for_update=True)
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
            match self._claim(token_state_id=token_state_id, operator_id=operator_id):
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
            return self._store_refreshed(
                token_state_id=token_state_id, operator_id=operator_id, claim=claim, refreshed=refreshed
            )
        except Exception as error:
            self._store_failure(token_state_id=token_state_id, claim_id=claim.claim_id, error=error)
            raise
        except BaseException:
            try:
                self._release_claim(token_state_id=token_state_id, claim_id=claim.claim_id)
            except Exception:
                logger.exception("Failed to release OAuth refresh claim for token state %s", token_state_id)
            raise
