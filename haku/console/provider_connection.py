"""Per-Operator OAuth connections to well-known external providers (Google today).

Each Operator links deploy-named, scope-specific Google connections through authorization-code +
PKCE flows, and the console stores and self-refreshes each refresh token independently in Postgres.
``ToolCallApplicationService`` reads the fresh access token via ``access_token_for`` before
executing a gmail/google_calendar call; the ``gmail`` and ``google_calendar`` in-process servers
are built per call from it.

Parallel to ``mcp_operator_oauth`` (per-Operator, Postgres-backed, self-refreshing), but for
fixed pre-registered clients: no Dynamic Client Registration and no authorization-server
metadata discovery. The provider catalog and non-secret metadata live in
``provider_connection_registry``; deploy config associates each connection with a named provider
instance whose client_id/secret come from environment variables and never enter the database.
"""

from __future__ import annotations

import datetime
import os
import secrets
from functools import partial
from typing import Annotated, Literal, cast
from urllib.parse import urlencode
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from mcp.client.auth.oauth2 import PKCEParameters
from mcp.shared.auth import OAuthToken
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import selectinload

from haku.console.config import ProviderOAuthClientConfig
from haku.console.console_events import ConsoleEventHubDep, OperatorConnectionChangedEvent
from haku.console.database_schema import ProviderConnection, ProviderConnectionFlow
from haku.console.deps import SettingsDep
from haku.console.mcp_config import (
    ConsoleConfigFile,
    OperatorConnectionDefinition,
    OperatorConnectionProviderDefinition,
)
from haku.console.oauth_connection_result import (
    OAuthConnectionFailed,
    OAuthConnectionResultStoreDep,
    OAuthConnectionSucceeded,
    bounded_result_message,
    result_redirect,
)
from haku.console.oauth_token_state import (
    OAuthRefreshFailureEpisode,
    PostgresOAuthTokenStateStore,
    new_oauth_token_state,
    refresh_failure_episode,
)
from haku.console.oauth_token_support import (
    OAuthTokenResponseError,
    parse_token_response,
    public_base_url,
    token_expires_at,
    token_request_headers,
)
from haku.console.operator_auth import OperatorActorDep
from haku.console.operator_identity_store import PostgresOperatorIdentityStore
from haku.console.provider_connection_registry import (
    PROVIDER_DESCRIPTORS,
    ProviderConnectionDescriptor,
    ProviderConnectionKind,
)

PROVIDER_CONNECTION_CALLBACK_PATH = "/api/provider-connections/callback"
_FLOW_TTL = datetime.timedelta(minutes=10)
_TOKEN_ENDPOINT_TIMEOUT_SECONDS = 10.0

router = APIRouter(tags=["operator-connections"])
UNPROVISIONED_DETAIL = "OAuth client not provisioned on this console; see the console deployment README."


class ProviderConnectionStatusBase(BaseModel):
    model_config = ConfigDict(json_schema_serialization_defaults_required=True)

    connection: str
    display_name: str
    provider: ProviderConnectionKind


class ProviderConnected(ProviderConnectionStatusBase):
    status: Literal["connected"] = "connected"
    connected_at: datetime.datetime
    # None when the token declares no expiry (OAuth `expires_in` absent).
    token_expires_at: datetime.datetime | None = None
    scope: str | None = None


class ProviderDegraded(ProviderConnectionStatusBase):
    status: Literal["degraded"] = "degraded"
    connected_at: datetime.datetime
    token_expires_at: datetime.datetime | None = None
    scope: str | None = None
    refresh_failure: OAuthRefreshFailureEpisode


class ProviderUnconnected(ProviderConnectionStatusBase):
    status: Literal["unconnected"] = "unconnected"


class ProviderUnprovisioned(ProviderConnectionStatusBase):
    status: Literal["unprovisioned"] = "unprovisioned"
    detail: str


# Discriminated on `status`, so the connected-only fields exist exactly when connected.
type ProviderConnectionStatus = Annotated[
    ProviderConnected | ProviderDegraded | ProviderUnconnected | ProviderUnprovisioned, Field(discriminator="status")
]


class ProviderConnectionStatusResponse(BaseModel):
    connections: list[ProviderConnectionStatus] = Field(default_factory=list)


class ProviderConnectionConnectResponse(BaseModel):
    connection: str
    provider: ProviderConnectionKind
    authorization_url: str
    expires_at: datetime.datetime


class _FlowState(BaseModel):
    """A ``ProviderConnectionFlow`` row read out before its session closes, carried across the
    authorization-code → token exchange (which must not hold a DB session open)."""

    connection_name: str
    provider_name: str
    provider: ProviderConnectionKind
    operator_id: UUID
    expires_at: datetime.datetime
    redirect_uri: str
    code_verifier: str
    scope: str | None = None


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


def _status_from_row(
    connection: str,
    definition: OperatorConnectionDefinition,
    provider: OperatorConnectionProviderDefinition,
    row: ProviderConnection | None,
    *,
    provisioned: bool,
) -> ProviderConnectionStatus:
    if not provisioned:
        return ProviderUnprovisioned(
            connection=connection,
            display_name=definition.display_name,
            provider=provider.kind,
            detail=UNPROVISIONED_DETAIL,
        )
    if row is None:
        return ProviderUnconnected(connection=connection, display_name=definition.display_name, provider=provider.kind)
    if row.provider_name != definition.provider or row.provider != provider.kind:
        raise RuntimeError(f"operator connection {connection!r} provider changed; disconnect it before continuing")
    if (failure := refresh_failure_episode(row.token_state)) is not None:
        return ProviderDegraded(
            connection=connection,
            display_name=definition.display_name,
            provider=provider.kind,
            connected_at=row.created_at,
            token_expires_at=row.token_state.token_expires_at,
            scope=row.token_state.scope,
            refresh_failure=failure,
        )
    return ProviderConnected(
        connection=connection,
        display_name=definition.display_name,
        provider=provider.kind,
        connected_at=row.created_at,
        token_expires_at=row.token_state.token_expires_at,
        scope=row.token_state.scope,
    )


def _token_request_data(
    descriptor: ProviderConnectionDescriptor, client: ProviderOAuthClientConfig, base: dict[str, str]
) -> dict[str, str]:
    data = dict(base)
    data["client_id"] = client.client_id
    # Google authenticates the client with the secret in the request body (client_secret_post).
    assert descriptor.token_endpoint_auth_method == "client_secret_post"
    data["client_secret"] = client.client_secret.get_secret_value()
    return data


async def _post_token(descriptor: ProviderConnectionDescriptor, data: dict[str, str]) -> httpx.Response:
    async with httpx.AsyncClient(timeout=_TOKEN_ENDPOINT_TIMEOUT_SECONDS) as http:
        return await http.post(descriptor.token_url, data=data, headers=token_request_headers())


async def _exchange_code(
    descriptor: ProviderConnectionDescriptor,
    client: ProviderOAuthClientConfig,
    *,
    code: str,
    redirect_uri: str,
    code_verifier: str,
) -> OAuthToken:
    data = _token_request_data(
        descriptor,
        client,
        {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "code_verifier": code_verifier,
        },
    )
    response = await _post_token(descriptor, data)
    try:
        return await parse_token_response(response, label=f"{descriptor.display_name} token exchange")
    except OAuthTokenResponseError as error:
        raise HTTPException(status_code=502, detail=str(error)) from error


async def _refresh_token(
    descriptor: ProviderConnectionDescriptor, client: ProviderOAuthClientConfig, refresh_token: str
) -> OAuthToken:
    data = _token_request_data(descriptor, client, {"grant_type": "refresh_token", "refresh_token": refresh_token})
    response = await _post_token(descriptor, data)
    return await parse_token_response(response, label=f"{descriptor.display_name} token refresh")


def load_provider_clients(config: ConsoleConfigFile) -> dict[str, ProviderOAuthClientConfig]:
    """Resolve configured provider client secrets once, skipping wholly absent optional clients."""
    clients: dict[str, ProviderOAuthClientConfig] = {}
    for name, definition in config.operator_connection_providers.items():
        client_id = os.environ.get(definition.client_id_env_var)
        client_secret = os.environ.get(definition.client_secret_env_var)
        if client_id is None and client_secret is None:
            continue
        if not client_id:
            raise RuntimeError(f"missing provider client id env var {definition.client_id_env_var} for {name!r}")
        if not client_secret:
            raise RuntimeError(
                f"missing provider client secret env var {definition.client_secret_env_var} for {name!r}"
            )
        clients[name] = ProviderOAuthClientConfig(client_id=client_id, client_secret=client_secret)
    return clients


class PostgresProviderConnectionStore:
    """Postgres-backed per-Operator provider connection store (connect/refresh/disconnect)."""

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        *,
        operator_identity_store: PostgresOperatorIdentityStore,
        token_states: PostgresOAuthTokenStateStore,
        provider_definitions: dict[str, OperatorConnectionProviderDefinition],
        provider_clients: dict[str, ProviderOAuthClientConfig],
        operator_connections: dict[str, OperatorConnectionDefinition],
    ) -> None:
        # Migrations are applied once at startup (database_migrate.apply_migrations), not here. The
        # engine/sessionmaker is created once in create_app and shared across every store.
        self._sessions = sessions
        self._operator_identity_store = operator_identity_store
        self._token_states = token_states
        self._provider_definitions = provider_definitions
        self._provider_clients = provider_clients
        self._operator_connections = operator_connections

    async def _require_definition(self, connection: str) -> OperatorConnectionDefinition:
        definition = self._operator_connections.get(connection)
        if definition is None:
            raise HTTPException(status_code=404, detail=f"operator connection {connection!r} is not configured")
        return definition

    async def _require_provider_definition(self, provider: str) -> OperatorConnectionProviderDefinition:
        definition = self._provider_definitions.get(provider)
        if definition is None:
            raise RuntimeError(f"operator connection provider {provider!r} is not configured")
        return definition

    async def is_connected(self, *, connection: str, operator_id: UUID) -> bool:
        """Read persisted connection presence without refreshing or returning its credential."""
        await self._require_definition(connection)
        async with self._sessions.begin() as session:
            await self._operator_identity_store.require_active_in_transaction(session, operator_id)
            return await session.get(ProviderConnection, (operator_id, connection)) is not None

    async def is_provisioned(self, *, connection: str) -> bool:
        """Whether the cataloged connection's provider OAuth client is installed."""
        return (await self._require_definition(connection)).provider in self._provider_clients

    @property
    async def cataloged_connections(self) -> list[tuple[str, OperatorConnectionDefinition]]:
        """All cataloged logical connections, in config order, including unprovisioned ones."""
        return list(self._operator_connections.items())

    async def _require_client(self, provider: str) -> ProviderOAuthClientConfig:
        client = self._provider_clients.get(provider)
        if client is None:
            raise HTTPException(status_code=404, detail=f"provider {provider} is not configured on this console")
        return client

    async def list_statuses(self, *, operator_id: UUID) -> ProviderConnectionStatusResponse:
        connections = await self.cataloged_connections
        async with self._sessions.begin() as session:
            await self._operator_identity_store.require_active_in_transaction(session, operator_id)
            if not connections:
                return ProviderConnectionStatusResponse()
            rows = (
                await session.scalars(
                    select(ProviderConnection)
                    .where(ProviderConnection.operator_id == operator_id)
                    .where(ProviderConnection.connection_name.in_([name for name, _ in connections]))
                    .options(selectinload(ProviderConnection.token_state))
                )
            ).all()
        by_connection = {row.connection_name: row for row in rows}
        return ProviderConnectionStatusResponse(
            connections=[
                _status_from_row(
                    name,
                    definition,
                    await self._require_provider_definition(definition.provider),
                    by_connection.get(name),
                    provisioned=definition.provider in self._provider_clients,
                )
                for name, definition in connections
            ]
        )

    async def connect_flow(
        self, *, connection: str, operator_id: UUID, public_base_url: str
    ) -> ProviderConnectionConnectResponse:
        definition = await self._require_definition(connection)
        provider = await self._require_provider_definition(definition.provider)
        descriptor = PROVIDER_DESCRIPTORS[provider.kind]
        client = await self._require_client(definition.provider)
        redirect_uri = f"{public_base_url.rstrip('/')}{PROVIDER_CONNECTION_CALLBACK_PATH}"
        pkce = PKCEParameters.generate()
        state = secrets.token_urlsafe(32)
        now = _now()
        expires_at = now + _FLOW_TTL
        params = {
            "response_type": "code",
            "client_id": client.client_id,
            "redirect_uri": redirect_uri,
            "state": state,
            "scope": " ".join(definition.scopes),
            "code_challenge": pkce.code_challenge,
            "code_challenge_method": "S256",
            **dict(descriptor.extra_auth_params),
        }
        authorization_url = f"{descriptor.authorize_url}?{urlencode(params)}"
        async with self._sessions.begin() as session:
            await self._operator_identity_store.require_active_in_transaction(session, operator_id)
            if await session.get(ProviderConnection, (operator_id, connection)) is not None:
                raise HTTPException(
                    status_code=409, detail=f"{definition.display_name} is already connected; disconnect it first"
                )
            await session.execute(delete(ProviderConnectionFlow).where(ProviderConnectionFlow.expires_at < now))
            await session.execute(
                delete(ProviderConnectionFlow)
                .where(ProviderConnectionFlow.operator_id == operator_id)
                .where(ProviderConnectionFlow.connection_name == connection)
            )
            session.add(
                ProviderConnectionFlow(
                    state=state,
                    operator_id=operator_id,
                    connection_name=connection,
                    provider_name=definition.provider,
                    provider=provider.kind,
                    created_at=now,
                    expires_at=expires_at,
                    redirect_uri=redirect_uri,
                    code_verifier=pkce.code_verifier,
                    scope=" ".join(definition.scopes),
                )
            )
        return ProviderConnectionConnectResponse(
            connection=connection, provider=provider.kind, authorization_url=authorization_url, expires_at=expires_at
        )

    async def complete_callback(self, *, state: str, code: str, operator_id: UUID) -> ProviderConnectionStatus:
        async with self._sessions.begin() as session:
            await self._operator_identity_store.require_active_in_transaction(session, operator_id)
            if (row := await session.get(ProviderConnectionFlow, state)) is None:
                raise HTTPException(status_code=404, detail="OAuth flow not found or already used")
            if row.operator_id != operator_id:
                raise HTTPException(status_code=403, detail="OAuth flow belongs to a different operator")
            flow = _FlowState(
                connection_name=row.connection_name,
                provider_name=row.provider_name,
                provider=row.provider,
                operator_id=row.operator_id,
                expires_at=row.expires_at,
                redirect_uri=row.redirect_uri,
                code_verifier=row.code_verifier,
                scope=row.scope,
            )
            await session.delete(row)
        now = _now()
        if flow.expires_at < now:
            raise HTTPException(status_code=410, detail="OAuth flow expired; start the connection again")
        descriptor = PROVIDER_DESCRIPTORS[flow.provider]
        definition = await self._require_definition(flow.connection_name)
        if definition.provider != flow.provider_name:
            raise HTTPException(status_code=409, detail="operator connection provider changed; start again")
        provider = await self._require_provider_definition(flow.provider_name)
        if provider.kind != flow.provider:
            raise HTTPException(status_code=409, detail="operator connection provider kind changed; start again")
        client = await self._require_client(flow.provider_name)
        token = await _exchange_code(
            descriptor, client, code=code, redirect_uri=flow.redirect_uri, code_verifier=flow.code_verifier
        )
        expires_at = token_expires_at(token, now)
        async with self._sessions.begin() as session:
            # The code exchange is external I/O. Make the active-status check and the persistence one
            # atomic final step, so a disable committed while it was in flight is observed here.
            await self._operator_identity_store.require_active_in_transaction(session, flow.operator_id)
            if (
                await session.get(ProviderConnection, (flow.operator_id, flow.connection_name), with_for_update=True)
                is not None
            ):
                raise HTTPException(
                    status_code=409, detail=f"{definition.display_name} is already connected; disconnect it first"
                )
            connection = ProviderConnection(
                operator_id=flow.operator_id,
                connection_name=flow.connection_name,
                provider_name=flow.provider_name,
                provider=flow.provider,
                created_at=now,
                token_state=new_oauth_token_state(
                    operator_id=flow.operator_id,
                    access_token=token.access_token,
                    refresh_token=token.refresh_token,
                    token_type=token.token_type,
                    scope=token.scope or flow.scope,
                    expires_at=expires_at,
                    now=now,
                ),
            )
            session.add(connection)
            return _status_from_row(flow.connection_name, definition, provider, connection, provisioned=True)

    async def disconnect(self, *, connection: str, operator_id: UUID) -> ProviderUnconnected:
        definition = await self._require_definition(connection)
        provider = await self._require_provider_definition(definition.provider)
        async with self._sessions.begin() as session:
            await self._operator_identity_store.require_active_in_transaction(session, operator_id)
            row = await session.get(ProviderConnection, (operator_id, connection), with_for_update=True)
            if row is not None:
                await session.delete(row)
        return ProviderUnconnected(connection=connection, display_name=definition.display_name, provider=provider.kind)

    async def access_token_for(self, *, connection: str, operator_id: UUID) -> str | None:
        definition = await self._require_definition(connection)
        provider = await self._require_provider_definition(definition.provider)
        descriptor = PROVIDER_DESCRIPTORS[provider.kind]
        client = await self._require_client(definition.provider)
        for attempt in range(2):
            async with self._sessions.begin() as session:
                await self._operator_identity_store.require_active_in_transaction(session, operator_id)
                row = await session.get(ProviderConnection, (operator_id, connection))
                if row is None:
                    return None
                if row.provider_name != definition.provider or row.provider != provider.kind:
                    raise RuntimeError(
                        f"operator connection {connection!r} provider changed; disconnect it before continuing"
                    )
                token_state_id = row.token_state_id

            access_token = await self._token_states.access_token_for(
                token_state_id=token_state_id,
                operator_id=operator_id,
                refresh=partial(_refresh_token, descriptor, client),
            )
            if access_token is not None or attempt:
                return access_token
        return None


def _store(request: Request) -> PostgresProviderConnectionStore:
    return cast(PostgresProviderConnectionStore, request.app.state.provider_connection_store)


ProviderConnectionStoreDep = Annotated[PostgresProviderConnectionStore, Depends(_store)]


@router.post("/api/operator-connections/{connection}/connect")
async def connect_provider_connection(
    connection: str, settings: SettingsDep, store: ProviderConnectionStoreDep, actor: OperatorActorDep
) -> ProviderConnectionConnectResponse:
    return await store.connect_flow(
        connection=connection, operator_id=actor.operator_id, public_base_url=public_base_url(settings)
    )


@router.delete("/api/operator-connections/{connection}")
async def disconnect_provider_connection(
    connection: str, store: ProviderConnectionStoreDep, event_hub: ConsoleEventHubDep, actor: OperatorActorDep
) -> ProviderUnconnected:
    operator_id = actor.operator_id
    status = await store.disconnect(connection=connection, operator_id=operator_id)
    await event_hub.broadcast(
        operator_id, [OperatorConnectionChangedEvent(connection=connection, status="disconnected")]
    )
    return status


@router.get(PROVIDER_CONNECTION_CALLBACK_PATH)
async def provider_connection_callback(
    *,
    store: ProviderConnectionStoreDep,
    result_store: OAuthConnectionResultStoreDep,
    event_hub: ConsoleEventHubDep,
    actor: OperatorActorDep,
    state: str | None = None,
    code: str | None = None,
    error: str | None = None,
) -> RedirectResponse:
    operator_id = actor.operator_id
    if error:
        return await result_redirect(
            result_store,
            operator_id=operator_id,
            result=OAuthConnectionFailed(
                title="Couldn't connect the account",
                message=bounded_result_message(f"Authorization failed: {error}", fallback="Authorization failed."),
            ),
        )
    if not state or not code:
        return await result_redirect(
            result_store,
            operator_id=operator_id,
            result=OAuthConnectionFailed(
                title="Couldn't connect the account", message="The authorization response was incomplete."
            ),
        )
    try:
        status = await store.complete_callback(state=state, code=code, operator_id=operator_id)
    except HTTPException as e:
        detail = e.detail if isinstance(e.detail, str) else "Connection failed."
        return await result_redirect(
            result_store,
            operator_id=operator_id,
            result=OAuthConnectionFailed(
                title="Couldn't connect the account",
                message=bounded_result_message(detail, fallback="Connection failed."),
            ),
        )
    await event_hub.broadcast(
        operator_id, [OperatorConnectionChangedEvent(connection=status.connection, status="connected")]
    )
    return await result_redirect(
        result_store,
        operator_id=operator_id,
        result=OAuthConnectionSucceeded(
            title=f"Connected to {status.display_name}", message="The account is now available in Haku Console."
        ),
    )
