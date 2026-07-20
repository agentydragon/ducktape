"""Shared persistence and refresh state machine for Operator OAuth tokens."""

from __future__ import annotations

import asyncio
import datetime
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from uuid import UUID, uuid4

from mcp.shared.auth import OAuthToken
from sqlalchemy.orm import Session, sessionmaker

from haku.console.database_schema import OAuthTokenState
from haku.console.oauth_token_support import token_expires_at, token_is_fresh
from haku.console.operator_identity_store import PostgresOperatorIdentityStore

logger = logging.getLogger(__name__)

type RefreshToken = Callable[[str], Awaitable[OAuthToken]]

_REFRESH_CLAIM_TTL = datetime.timedelta(seconds=30)
_REFRESH_CLAIM_POLL_SECONDS = 0.05


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


type _ClaimResult = _Fresh | _Missing | _Wait | _Claim


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
                case _Claim() as claim:
                    break
        try:
            refreshed = await refresh(claim.refresh_token)
            return self._store_refreshed(
                token_state_id=token_state_id, operator_id=operator_id, claim=claim, refreshed=refreshed
            )
        except BaseException:
            try:
                self._release_claim(token_state_id=token_state_id, claim_id=claim.claim_id)
            except Exception:
                logger.exception("Failed to release OAuth refresh claim for token state %s", token_state_id)
            raise
