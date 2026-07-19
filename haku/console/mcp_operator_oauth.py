"""Operator OAuth account linkage for connected MCP servers.

Some MCP servers execute a tool call as *the operator's own account* rather than under a
static console-held bearer (e.g. `kubectl-passthrough-mcp`, which runs kubectl as the
approving operator's cluster-admin identity). For those, the operator links their account
once through an OAuth authorization-code + PKCE flow; the console runs Dynamic Client
Registration (or uses a pre-registered `static_client_id`), stores the resulting token
association, and refreshes it as needed. This module owns that flow, its Postgres-backed
storage, and the connect/disconnect/callback endpoints. `ToolCallApplicationService`
consumes the linked token via `access_token_for` before executing an approved call; the
catalog of which servers use operator OAuth lives in `mcp_config`.
"""

from __future__ import annotations

import base64
import datetime
import secrets
from typing import Annotated, Literal, cast
from urllib.parse import quote, urlencode, urljoin
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi_csrf_protect import CsrfProtect
from mcp.client.auth.oauth2 import PKCEParameters
from mcp.client.auth.utils import (
    build_oauth_authorization_server_metadata_discovery_urls,
    build_protected_resource_metadata_discovery_urls,
    extract_resource_metadata_from_www_auth,
    extract_scope_from_www_auth,
    get_client_metadata_scopes,
    handle_auth_metadata_response,
    handle_protected_resource_response,
    handle_registration_response,
)
from mcp.client.streamable_http import MCP_PROTOCOL_VERSION
from mcp.shared.auth import (
    OAuthClientInformationFull,
    OAuthClientMetadata,
    OAuthMetadata,
    OAuthToken,
    ProtectedResourceMetadata,
)
from mcp.shared.auth_utils import check_resource_allowed, resource_url_from_server_url
from mcp.types import LATEST_PROTOCOL_VERSION
from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.orm import Session, sessionmaker

from haku.console import operator_auth
from haku.console.console_events import ConsoleEventHubDep, McpOperatorAuthChangedEvent
from haku.console.database_schema import McpOperatorOAuthAssociation, McpOperatorOAuthFlow
from haku.console.deps import SettingsDep
from haku.console.mcp_config import (
    McpServerEntry,
    McpServerNotFoundError,
    RemoteMcpBackend,
    RemoteServerOAuthAuth,
    _load_servers,
    _operator_oauth_enabled,
    _server_entry,
)
from haku.console.oauth_callback_page import render_oauth_callback_page
from haku.console.oauth_token_support import parse_token_response, public_base_url, token_expires_at, token_is_fresh
from haku.console.operator_auth import OperatorActorDep
from haku.console.operator_identity_store import PostgresOperatorIdentityStore

Csrf = Annotated[CsrfProtect, Depends()]

MCP_OPERATOR_AUTH_CALLBACK_PATH = "/api/mcp/operator-auth/callback"
MCP_OPERATOR_AUTH_FLOW_TTL = datetime.timedelta(minutes=10)

router = APIRouter(tags=["mcp-operator-oauth"])


class McpOperatorAuthStatusBase(BaseModel):
    server_id: str
    # The operator's human username (preferred_username), for display. Durable association
    # ownership is keyed internally by canonical Operator UUID, which is not exposed in this API.
    username: str


class McpOperatorAuthConnected(McpOperatorAuthStatusBase):
    status: Literal["connected"] = "connected"
    connected_at: datetime.datetime
    # None when the linked token declares no expiry (OAuth `expires_in` absent).
    token_expires_at: datetime.datetime | None = None
    scope: str | None = None


class McpOperatorAuthUnconnected(McpOperatorAuthStatusBase):
    status: Literal["unconnected"] = "unconnected"


# Discriminated on `status`, so the connected-only fields (connected_at/token_expires_at/
# scope) exist exactly when connected — no "unconnected with a connected_at" nonsense state.
type McpOperatorAuthStatus = Annotated[
    McpOperatorAuthConnected | McpOperatorAuthUnconnected, Field(discriminator="status")
]


class McpOperatorAuthStatusResponse(BaseModel):
    associations: list[McpOperatorAuthStatus] = Field(default_factory=list)


class McpOperatorAuthConnectResponse(BaseModel):
    server_id: str
    authorization_url: str
    expires_at: datetime.datetime


class _OperatorOAuthTokenClient(BaseModel):
    """Token-endpoint call parameters shared by the auth-code exchange and the refresh.
    Detached from the ORM row so the network call runs with no DB session held open."""

    client_id: str
    client_secret: str | None = None
    token_endpoint_auth_method: str | None = None
    token_endpoint: str
    resource: str | None = None


class OperatorOAuthFlowState(_OperatorOAuthTokenClient):
    """An `McpOperatorOAuthFlow` row read out before its session closes, carried across the
    authorization-code → token exchange (which must not hold a DB session open)."""

    server_id: str
    operator_id: UUID
    expires_at: datetime.datetime
    redirect_uri: str
    code_verifier: str
    client_secret_expires_at: int | None = None
    scope: str | None = None

    @classmethod
    def from_row(cls, row: McpOperatorOAuthFlow) -> OperatorOAuthFlowState:
        return cls(
            client_id=row.client_id,
            client_secret=row.client_secret,
            token_endpoint_auth_method=row.token_endpoint_auth_method,
            token_endpoint=row.token_endpoint,
            resource=row.resource,
            server_id=row.server_id,
            operator_id=row.operator_id,
            expires_at=row.expires_at,
            redirect_uri=row.redirect_uri,
            code_verifier=row.code_verifier,
            client_secret_expires_at=row.client_secret_expires_at,
            scope=row.scope,
        )


class OperatorOAuthRefreshState(_OperatorOAuthTokenClient):
    """An `McpOperatorOAuthAssociation` row's refresh inputs read out before its session
    closes, carried across the token-refresh network call."""

    association_id: UUID
    token_revision: int
    refresh_token: str | None = None

    @classmethod
    def from_row(cls, row: McpOperatorOAuthAssociation) -> OperatorOAuthRefreshState:
        return cls(
            association_id=row.association_id,
            token_revision=row.token_revision,
            client_id=row.client_id,
            client_secret=row.client_secret,
            token_endpoint_auth_method=row.token_endpoint_auth_method,
            token_endpoint=row.token_endpoint,
            resource=row.resource,
            refresh_token=row.refresh_token,
        )

    def still_matches(self, row: McpOperatorOAuthAssociation) -> bool:
        """Whether ``row`` is exactly the credential generation this refresh used."""
        return (
            row.association_id == self.association_id
            and row.token_revision == self.token_revision
            and row.client_id == self.client_id
            and row.client_secret == self.client_secret
            and row.token_endpoint_auth_method == self.token_endpoint_auth_method
            and row.token_endpoint == self.token_endpoint
            and row.resource == self.resource
            and row.refresh_token == self.refresh_token
        )


class _BuiltOperatorOAuthFlow(BaseModel):
    state: str
    authorization_url: str
    expires_at: datetime.datetime
    redirect_uri: str
    code_verifier: str
    client_info: OAuthClientInformationFull
    token_endpoint: str
    resource: str | None = None
    scope: str | None = None


class PostgresMcpOperatorOAuthStore:
    """Postgres-backed operator OAuth association store for connected MCP servers."""

    def __init__(
        self, sessions: sessionmaker[Session], *, operator_identity_store: PostgresOperatorIdentityStore
    ) -> None:
        # Migrations are applied once at startup (haku.console.database_migrate.apply_migrations), not
        # here — constructing the store neither connects nor mutates schema. The engine/sessionmaker is
        # created once in create_app and shared across every store.
        self._sessions = sessions
        self._operator_identity_store = operator_identity_store

    def list_statuses(
        self, *, servers: list[McpServerEntry], operator_id: UUID, username: str
    ) -> McpOperatorAuthStatusResponse:
        oauth_servers = [server for server in servers if _operator_oauth_enabled(server)]
        server_ids = [server.id for server in oauth_servers]
        with self._sessions.begin() as session:
            self._operator_identity_store.require_active_in_transaction(session, operator_id)
            if not oauth_servers:
                return McpOperatorAuthStatusResponse()
            rows = session.scalars(
                select(McpOperatorOAuthAssociation)
                .where(McpOperatorOAuthAssociation.operator_id == operator_id)
                .where(McpOperatorOAuthAssociation.server_id.in_(server_ids))
            ).all()
        by_server = {row.server_id: row for row in rows}
        return McpOperatorAuthStatusResponse(
            associations=[
                _oauth_status_from_row(server.id, username, by_server.get(server.id)) for server in oauth_servers
            ]
        )

    async def connect_flow(
        self, *, server: McpServerEntry, operator_id: UUID, public_base_url: str
    ) -> McpOperatorAuthConnectResponse:
        if not _operator_oauth_enabled(server):
            raise HTTPException(status_code=404, detail=f"MCP server {server.id} does not use operator OAuth")
        with self._sessions.begin() as session:
            self._operator_identity_store.require_active_in_transaction(session, operator_id)
            existing = session.get(McpOperatorOAuthAssociation, (server.id, operator_id))
            if existing is not None:
                raise HTTPException(
                    status_code=409, detail=f"MCP server {server.id} is already connected; disconnect it first"
                )
        flow = await _build_operator_oauth_flow(server, public_base_url.rstrip("/"))
        with self._sessions.begin() as session:
            # Metadata discovery and DCR are external I/O. Revalidate under the Operator row lock
            # before persisting a flow so a disable committed while they ran wins this race.
            self._operator_identity_store.require_active_in_transaction(session, operator_id)
            now = datetime.datetime.now(datetime.UTC)
            session.execute(delete(McpOperatorOAuthFlow).where(McpOperatorOAuthFlow.expires_at < now))
            session.execute(
                delete(McpOperatorOAuthFlow)
                .where(McpOperatorOAuthFlow.server_id == server.id)
                .where(McpOperatorOAuthFlow.operator_id == operator_id)
            )
            session.add(
                McpOperatorOAuthFlow(
                    state=flow.state,
                    server_id=server.id,
                    operator_id=operator_id,
                    created_at=now,
                    expires_at=flow.expires_at,
                    redirect_uri=flow.redirect_uri,
                    code_verifier=flow.code_verifier,
                    client_id=flow.client_info.client_id or "",
                    client_secret=flow.client_info.client_secret,
                    client_secret_expires_at=flow.client_info.client_secret_expires_at,
                    token_endpoint_auth_method=flow.client_info.token_endpoint_auth_method,
                    token_endpoint=flow.token_endpoint,
                    resource=flow.resource,
                    scope=flow.scope,
                )
            )
        return McpOperatorAuthConnectResponse(
            server_id=server.id, authorization_url=flow.authorization_url, expires_at=flow.expires_at
        )

    async def complete_callback(
        self, *, state: str, code: str, operator_id: UUID, username: str
    ) -> McpOperatorAuthStatus:
        with self._sessions.begin() as session:
            self._operator_identity_store.require_active_in_transaction(session, operator_id)
            if (row := session.get(McpOperatorOAuthFlow, state)) is None:
                raise HTTPException(status_code=404, detail="OAuth flow not found or already used")
            if row.operator_id != operator_id:
                raise HTTPException(status_code=403, detail="OAuth flow belongs to a different operator")
            session.delete(row)
            flow = OperatorOAuthFlowState.from_row(row)
        now = datetime.datetime.now(datetime.UTC)
        if flow.expires_at < now:
            raise HTTPException(status_code=410, detail="OAuth flow expired; start connection again")
        token = await _exchange_operator_oauth_code(flow, code)
        expires_at = token_expires_at(token, now)
        with self._sessions.begin() as session:
            # The code exchange is external I/O. Make active status and association persistence one
            # atomic final step; never store a token if a disable committed while it was in flight.
            self._operator_identity_store.require_active_in_transaction(session, flow.operator_id)
            existing = session.get(
                McpOperatorOAuthAssociation, (flow.server_id, flow.operator_id), with_for_update=True
            )
            if existing is not None:
                raise HTTPException(
                    status_code=409, detail=f"MCP server {flow.server_id} is already connected; disconnect it first"
                )
            existing = McpOperatorOAuthAssociation(
                server_id=flow.server_id,
                operator_id=flow.operator_id,
                created_at=now,
                updated_at=now,
                client_id=flow.client_id,
                client_secret=flow.client_secret,
                client_secret_expires_at=flow.client_secret_expires_at,
                token_endpoint_auth_method=flow.token_endpoint_auth_method,
                token_endpoint=flow.token_endpoint,
                resource=flow.resource,
                access_token=token.access_token,
                refresh_token=token.refresh_token,
                token_type=token.token_type,
                scope=token.scope or flow.scope,
                token_expires_at=expires_at,
            )
            session.add(existing)
            return _oauth_status_from_row(flow.server_id, username, existing)

    def disconnect(self, *, server_id: str, operator_id: UUID) -> None:
        with self._sessions.begin() as session:
            self._operator_identity_store.require_active_in_transaction(session, operator_id)
            row = session.get(McpOperatorOAuthAssociation, (server_id, operator_id), with_for_update=True)
            if row is not None:
                session.delete(row)

    async def access_token_for(self, *, server: McpServerEntry, operator_id: UUID) -> str | None:
        if not _operator_oauth_enabled(server):
            return None
        with self._sessions.begin() as session:
            # Keep the status check and fresh-token read under one Operator row lock. This avoids
            # returning a capability after a disable has already committed.
            self._operator_identity_store.require_active_in_transaction(session, operator_id)
            row = session.get(McpOperatorOAuthAssociation, (server.id, operator_id), with_for_update=True)
            if row is None:
                return None
            now = datetime.datetime.now(datetime.UTC)
            if token_is_fresh(row.token_expires_at, now):
                return row.access_token
            if not row.refresh_token:
                return None
            snapshot = OperatorOAuthRefreshState.from_row(row)
        refreshed = await _refresh_operator_oauth_token(snapshot)
        expires_at = token_expires_at(refreshed, datetime.datetime.now(datetime.UTC))
        with self._sessions.begin() as session:
            # Refresh is external I/O. Revalidate in the same transaction as both the token write
            # and return so a disable committed during refresh cannot produce a usable token.
            self._operator_identity_store.require_active_in_transaction(session, operator_id)
            row = session.get(McpOperatorOAuthAssociation, (server.id, operator_id), with_for_update=True)
            if row is None:
                return None
            if not snapshot.still_matches(row):
                # The external call used a credential snapshot after releasing the association
                # lock. A concurrent refresh or disconnect/reconnect owns the row now; never let
                # this stale response overwrite it. Its current fresh token is safe to use, while
                # an already-expired replacement must be retried from a new snapshot.
                if token_is_fresh(row.token_expires_at, datetime.datetime.now(datetime.UTC)):
                    return row.access_token
                raise RuntimeError("MCP OAuth association changed during refresh; retry the tool call")
            row.updated_at = datetime.datetime.now(datetime.UTC)
            row.access_token = refreshed.access_token
            row.refresh_token = refreshed.refresh_token or row.refresh_token
            row.token_type = refreshed.token_type
            row.scope = refreshed.scope or row.scope
            row.token_expires_at = expires_at
            row.token_revision += 1
            return row.access_token


def _oauth_store(request: Request) -> PostgresMcpOperatorOAuthStore:
    return cast(PostgresMcpOperatorOAuthStore, request.app.state.mcp_operator_oauth_store)


OAuthStoreDep = Annotated[PostgresMcpOperatorOAuthStore, Depends(_oauth_store)]


def _operator_username(request: Request) -> str:
    """The current operator's human username, for display in status responses / callback messages.
    Display only (never a key), so a benign label is fine when the username claim is absent."""
    return operator_auth.operator_username(request) or "operator"


def _oauth_status_from_row(
    server_id: str, username: str, row: McpOperatorOAuthAssociation | None
) -> McpOperatorAuthStatus:
    if row is None:
        return McpOperatorAuthUnconnected(server_id=server_id, username=username)
    return McpOperatorAuthConnected(
        server_id=server_id,
        username=username,
        connected_at=row.created_at,
        token_expires_at=row.token_expires_at,
        scope=row.scope,
    )


def _metadata_request_headers() -> dict[str, str]:
    return {MCP_PROTOCOL_VERSION: LATEST_PROTOCOL_VERSION}


async def _discover_protected_resource(
    client: httpx.AsyncClient, server_url: str, auth_probe: httpx.Response
) -> ProtectedResourceMetadata | None:
    metadata_url = extract_resource_metadata_from_www_auth(auth_probe)
    for url in build_protected_resource_metadata_discovery_urls(metadata_url, server_url):
        response = await client.get(url, headers=_metadata_request_headers())
        if metadata := await handle_protected_resource_response(response):
            return metadata
    return None


async def _discover_oauth_metadata(
    client: httpx.AsyncClient, server_url: str, resource_metadata: ProtectedResourceMetadata | None
) -> OAuthMetadata | None:
    auth_server_url = str(resource_metadata.authorization_servers[0]) if resource_metadata else None
    for url in build_oauth_authorization_server_metadata_discovery_urls(auth_server_url, server_url):
        response = await client.get(url, headers=_metadata_request_headers())
        ok, metadata = await handle_auth_metadata_response(response)
        if metadata:
            return metadata
        if not ok:
            break
    return None


def _resource_for_oauth(server_url: str, resource_metadata: ProtectedResourceMetadata | None) -> str | None:
    if resource_metadata is None:
        return None
    requested = resource_url_from_server_url(server_url)
    configured = str(resource_metadata.resource)
    if check_resource_allowed(requested_resource=requested, configured_resource=configured):
        return configured
    return requested


async def _register_oauth_client(
    client: httpx.AsyncClient,
    server_url: str,
    oauth_metadata: OAuthMetadata | None,
    client_metadata: OAuthClientMetadata,
) -> OAuthClientInformationFull:
    if oauth_metadata and oauth_metadata.registration_endpoint:
        registration_url = str(oauth_metadata.registration_endpoint)
    else:
        registration_url = urljoin(_authorization_base_url(server_url), "/register")
    response = await client.post(
        registration_url,
        json=client_metadata.model_dump(by_alias=True, mode="json", exclude_none=True),
        headers={"Content-Type": "application/json"},
    )
    return await handle_registration_response(response)


def _authorization_base_url(server_url: str) -> str:
    parsed = httpx.URL(server_url)
    return f"{parsed.scheme}://{parsed.host}{f':{parsed.port}' if parsed.port else ''}"


class _ResolvedOAuthClient(BaseModel):
    """The authorization-server metadata and registered client obtained by probing an MCP
    server — everything `_build_operator_oauth_flow` needs to assemble the auth request."""

    client_info: OAuthClientInformationFull
    oauth_metadata: OAuthMetadata | None = None
    resource_metadata: ProtectedResourceMetadata | None = None
    scope: str | None = None


async def _resolve_operator_oauth_client(
    server: McpServerEntry, server_url: str, redirect_uri: str
) -> _ResolvedOAuthClient:
    """Probe the MCP server, discover its authorization-server metadata, and obtain a client
    registration — a configured `static_client_id`, or Dynamic Client Registration. All the
    network I/O of starting an operator OAuth flow lives here."""
    assert isinstance(server.backend, RemoteMcpBackend)
    assert isinstance(server.backend.auth, RemoteServerOAuthAuth)
    # Bind to a local so the RemoteServerOAuthAuth narrowing survives the awaits below.
    oauth = server.backend.auth
    try:
        async with httpx.AsyncClient(follow_redirects=False, timeout=10.0) as client:
            auth_probe = await client.get(server_url, headers=_metadata_request_headers())
            resource_metadata = await _discover_protected_resource(client, server_url, auth_probe)
            oauth_metadata = await _discover_oauth_metadata(client, server_url, resource_metadata)
            scope = (
                " ".join(oauth.scopes)
                if oauth.scopes is not None
                else get_client_metadata_scopes(
                    extract_scope_from_www_auth(auth_probe), resource_metadata, oauth_metadata
                )
            )
            client_metadata = OAuthClientMetadata(
                client_name=oauth.client_name,
                redirect_uris=[redirect_uri],
                grant_types=["authorization_code", "refresh_token"],
                response_types=["code"],
                scope=scope,
            )
            if oauth.static_client_id:
                client_info = OAuthClientInformationFull(
                    client_id=oauth.static_client_id,
                    redirect_uris=client_metadata.redirect_uris,
                    grant_types=client_metadata.grant_types,
                    response_types=client_metadata.response_types,
                    scope=client_metadata.scope,
                    client_name=client_metadata.client_name,
                )
            else:
                client_info = await _register_oauth_client(client, server_url, oauth_metadata, client_metadata)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"failed to start MCP OAuth flow for {server.id}: {e}") from e
    if not client_info.client_id:
        raise HTTPException(status_code=502, detail=f"MCP OAuth registration for {server.id} did not return client_id")
    return _ResolvedOAuthClient(
        client_info=client_info, oauth_metadata=oauth_metadata, resource_metadata=resource_metadata, scope=scope
    )


async def _build_operator_oauth_flow(server: McpServerEntry, public_base_url: str) -> _BuiltOperatorOAuthFlow:
    assert isinstance(server.backend, RemoteMcpBackend)
    assert isinstance(server.backend.auth, RemoteServerOAuthAuth)
    # remote_server_oauth is meaningless for an in-process server (there's no remote authorization
    # server to run DCR/metadata discovery against); bind server_url to a local for stable
    # mypy narrowing across the awaits.
    server_url = server.backend.url
    redirect_uri = f"{public_base_url}{MCP_OPERATOR_AUTH_CALLBACK_PATH}"
    resolved = await _resolve_operator_oauth_client(server, server_url, redirect_uri)
    client_info = resolved.client_info
    oauth_metadata = resolved.oauth_metadata
    resource_metadata = resolved.resource_metadata
    scope = resolved.scope
    pkce = PKCEParameters.generate()
    state = secrets.token_urlsafe(32)
    if oauth_metadata and oauth_metadata.authorization_endpoint:
        auth_endpoint = str(oauth_metadata.authorization_endpoint)
    else:
        auth_endpoint = urljoin(_authorization_base_url(server_url), "/authorize")
    if oauth_metadata and oauth_metadata.token_endpoint:
        token_endpoint = str(oauth_metadata.token_endpoint)
    else:
        token_endpoint = urljoin(_authorization_base_url(server_url), "/token")
    resource = _resource_for_oauth(server_url, resource_metadata)
    params = {
        "response_type": "code",
        "client_id": client_info.client_id,
        "redirect_uri": redirect_uri,
        "state": state,
        "code_challenge": pkce.code_challenge,
        "code_challenge_method": "S256",
    }
    if resource:
        params["resource"] = resource
    if scope:
        params["scope"] = scope
    return _BuiltOperatorOAuthFlow(
        state=state,
        authorization_url=f"{auth_endpoint}?{urlencode(params)}",
        expires_at=datetime.datetime.now(datetime.UTC) + MCP_OPERATOR_AUTH_FLOW_TTL,
        redirect_uri=redirect_uri,
        code_verifier=pkce.code_verifier,
        client_info=client_info,
        token_endpoint=token_endpoint,
        resource=resource,
        scope=scope,
    )


def _token_request_auth(
    data: dict[str, str], client: _OperatorOAuthTokenClient
) -> tuple[dict[str, str], dict[str, str]]:
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    if client.token_endpoint_auth_method == "client_secret_basic" and client.client_secret:
        encoded_id = quote(client.client_id, safe="")
        encoded_secret = quote(client.client_secret, safe="")
        credentials = base64.b64encode(f"{encoded_id}:{encoded_secret}".encode()).decode()
        headers["Authorization"] = f"Basic {credentials}"
    elif client.token_endpoint_auth_method == "client_secret_post" and client.client_secret:
        data["client_secret"] = client.client_secret
    return data, headers


async def _exchange_operator_oauth_code(flow: OperatorOAuthFlowState, code: str) -> OAuthToken:
    data: dict[str, str] = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": flow.redirect_uri,
        "client_id": flow.client_id,
        "code_verifier": flow.code_verifier,
    }
    if flow.resource:
        data["resource"] = flow.resource
    data, headers = _token_request_auth(data, flow)
    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.post(flow.token_endpoint, data=data, headers=headers)
    return await parse_token_response(
        response, label="MCP OAuth token exchange", error=lambda m: HTTPException(status_code=502, detail=m)
    )


async def _refresh_operator_oauth_token(association: OperatorOAuthRefreshState) -> OAuthToken:
    refresh_token = association.refresh_token
    if not refresh_token:
        raise RuntimeError("MCP OAuth association has no refresh token; reconnect in the console")
    data: dict[str, str] = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": association.client_id,
    }
    if association.resource:
        data["resource"] = association.resource
    data, headers = _token_request_auth(data, association)
    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.post(association.token_endpoint, data=data, headers=headers)
    return await parse_token_response(response, label="MCP OAuth token refresh", error=RuntimeError)


def _oauth_callback_response(ok: bool, message: str, *, status_code: int = 200) -> HTMLResponse:
    title = "MCP account connected" if ok else "MCP account connection failed"
    return render_oauth_callback_page(title, message, status_code=status_code)


@router.get("/api/mcp/operator-auth")
async def mcp_operator_auth_statuses(
    request: Request, settings: SettingsDep, oauth_store: OAuthStoreDep, actor: OperatorActorDep
) -> McpOperatorAuthStatusResponse:
    return oauth_store.list_statuses(
        servers=_load_servers(settings), operator_id=actor.operator_id, username=_operator_username(request)
    )


@router.post("/api/mcp/operator-auth/{server_id}/connect")
async def connect_mcp_operator_auth(
    server_id: str,
    request: Request,
    csrf_protect: Csrf,
    settings: SettingsDep,
    oauth_store: OAuthStoreDep,
    actor: OperatorActorDep,
) -> McpOperatorAuthConnectResponse:
    await csrf_protect.validate_csrf(request)
    try:
        server = _server_entry(settings, server_id)
    except McpServerNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    return await oauth_store.connect_flow(
        server=server, operator_id=actor.operator_id, public_base_url=public_base_url(settings)
    )


@router.delete("/api/mcp/operator-auth/{server_id}")
async def disconnect_mcp_operator_auth(
    server_id: str,
    request: Request,
    csrf_protect: Csrf,
    oauth_store: OAuthStoreDep,
    event_hub: ConsoleEventHubDep,
    actor: OperatorActorDep,
) -> McpOperatorAuthUnconnected:
    await csrf_protect.validate_csrf(request)
    operator_id = actor.operator_id
    oauth_store.disconnect(server_id=server_id, operator_id=operator_id)
    await event_hub.broadcast(operator_id, [McpOperatorAuthChangedEvent(server_id=server_id, status="disconnected")])
    return McpOperatorAuthUnconnected(server_id=server_id, username=_operator_username(request))


@router.get(MCP_OPERATOR_AUTH_CALLBACK_PATH)
async def mcp_operator_auth_callback(
    request: Request,
    oauth_store: OAuthStoreDep,
    event_hub: ConsoleEventHubDep,
    actor: OperatorActorDep,
    state: str | None = None,
    code: str | None = None,
    error: str | None = None,
) -> HTMLResponse:
    if error:
        return _oauth_callback_response(False, f"MCP OAuth authorization failed: {error}", status_code=400)
    if not state or not code:
        return _oauth_callback_response(False, "MCP OAuth callback is missing state or code.", status_code=400)
    operator_id = actor.operator_id
    try:
        status = await oauth_store.complete_callback(
            state=state, code=code, operator_id=operator_id, username=_operator_username(request)
        )
    except HTTPException as e:
        detail = e.detail if isinstance(e.detail, str) else "MCP OAuth callback failed."
        return _oauth_callback_response(False, detail, status_code=e.status_code)
    await event_hub.broadcast(
        operator_id, [McpOperatorAuthChangedEvent(server_id=status.server_id, status="connected")]
    )
    return _oauth_callback_response(True, f"Connected {status.server_id} for {_operator_username(request)}.")
