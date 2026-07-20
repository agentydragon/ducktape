"""The acting Operator's own Authentik token, captured at browser login and self-refreshed.

The `hostexec` server exchanges this token for a short-lived per-host token, so the operator acts
under their own Authentik identity — no bespoke console key. Mirrors the provider-connection store's
Postgres-backed self-refresh, but the token is captured at browser login (offline_access) rather than
a separate connect flow, so there is no flow table and exactly one row per Operator. The operator-OIDC
client (id + secret) refreshes it; those come from Settings and are never persisted here.
"""

from __future__ import annotations

import datetime
from uuid import UUID

import httpx
from mcp.shared.auth import OAuthToken
from sqlalchemy.orm import Session, sessionmaker

from haku.console.database_schema import OperatorAuthentikToken
from haku.console.oauth_token_state import (
    PostgresOAuthTokenStateStore,
    new_oauth_token_state,
    replace_oauth_token_state,
)
from haku.console.oauth_token_support import parse_token_response
from haku.console.operator_identity_store import PostgresOperatorIdentityStore
from mcp_infra.authentik_auth.config import authentik_token_endpoint_for_issuer

_TOKEN_ENDPOINT_TIMEOUT_SECONDS = 10.0


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


class PostgresAuthentikOperatorTokenStore:
    """Stores and self-refreshes each Operator's own Authentik access/refresh token."""

    def __init__(
        self,
        sessions: sessionmaker[Session],
        *,
        operator_identity_store: PostgresOperatorIdentityStore,
        token_states: PostgresOAuthTokenStateStore,
        client_id: str,
        client_secret: str,
        issuer: str,
    ) -> None:
        # Migrations are applied once at startup (database_migrate.apply_migrations), not here. The
        # engine/sessionmaker is created once in create_app and shared across every store.
        self._sessions = sessions
        self._operator_identity_store = operator_identity_store
        self._token_states = token_states
        self._client_id = client_id
        self._client_secret = client_secret
        # The Authentik token endpoint is derived lazily (only on refresh, which only happens when
        # hostexec is actually used), so constructing the store never requires an Authentik-shaped
        # issuer — a non-Authentik operator OIDC (e.g. a hermetic test IdP) that never refreshes is fine.
        self._issuer = issuer

    def store_login_token(
        self,
        *,
        operator_id: UUID,
        access_token: str,
        refresh_token: str | None,
        token_type: str,
        scope: str | None,
        expires_at: datetime.datetime | None,
    ) -> None:
        """Upsert the operator's token captured at browser login (one row per Operator)."""
        now = _now()
        with self._sessions.begin() as session:
            self._operator_identity_store.require_active_in_transaction(session, operator_id)
            row = session.get(OperatorAuthentikToken, operator_id, with_for_update=True)
            if row is None:
                session.add(
                    OperatorAuthentikToken(
                        operator_id=operator_id,
                        created_at=now,
                        token_state=new_oauth_token_state(
                            operator_id=operator_id,
                            access_token=access_token,
                            refresh_token=refresh_token,
                            token_type=token_type,
                            scope=scope,
                            expires_at=expires_at,
                            now=now,
                        ),
                    )
                )
                return
            replace_oauth_token_state(
                row.token_state,
                access_token=access_token,
                refresh_token=refresh_token,
                token_type=token_type,
                scope=scope,
                expires_at=expires_at,
                now=now,
            )

    async def _refresh(self, refresh_token: str) -> OAuthToken:
        data = {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": self._client_id,
            "client_secret": self._client_secret,
        }
        async with httpx.AsyncClient(timeout=_TOKEN_ENDPOINT_TIMEOUT_SECONDS) as http:
            response = await http.post(authentik_token_endpoint_for_issuer(self._issuer), data=data)
        return await parse_token_response(response, label="Authentik operator token refresh", error=RuntimeError)

    async def access_token_for(self, *, operator_id: UUID) -> str | None:
        with self._sessions.begin() as session:
            self._operator_identity_store.require_active_in_transaction(session, operator_id)
            row = session.get(OperatorAuthentikToken, operator_id)
            if row is None:
                return None
            token_state_id = row.token_state_id
        return await self._token_states.access_token_for(
            token_state_id=token_state_id, operator_id=operator_id, refresh=self._refresh
        )
