"""Operator OAuth account linkage for connected MCP servers.

Some MCP servers execute a tool call as *the operator's own account* rather than under a
static console-held bearer (e.g. `kubectl-passthrough-mcp`, which runs kubectl as the
approving operator's cluster-admin identity). For those, the operator links their account
once through an OAuth authorization-code + PKCE flow; the console runs Dynamic Client
Registration (or uses a pre-registered `static_client_id`), stores the resulting token
association, and refreshes it as needed. This module owns that flow, its Postgres-backed
storage, and the connect/disconnect/callback endpoints. The approval router
(`mcp_approval`) consumes the linked token via `access_token_for` when it executes an
approved call; the catalog of which servers use operator OAuth lives in `mcp_config`.
"""

from __future__ import annotations

import base64
import datetime
import html
import secrets
from collections.abc import Mapping
from pathlib import Path
from string import Template
from typing import Annotated, Any, Literal, cast
from urllib.parse import quote, urlencode, urljoin

import httpx
import jwt
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
    handle_token_response_scopes,
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
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import create_engine, delete, select
from sqlalchemy.orm import sessionmaker

from haku.console import operator_auth
from haku.console.config import Settings
from haku.console.console_events import ConsoleEventHubDep, McpOperatorAuthChangedEvent
from haku.console.database_schema import McpAgentOperator, McpOperatorOAuthAssociation, McpOperatorOAuthFlow
from haku.console.deps import SettingsDep
from haku.console.mcp_config import McpServerEntry, _load_servers, _operator_oauth_enabled, _server_entry

Csrf = Annotated[CsrfProtect, Depends()]

MCP_OPERATOR_AUTH_CALLBACK_PATH = "/api/mcp/operator-auth/callback"
MCP_OPERATOR_AUTH_FLOW_TTL = datetime.timedelta(minutes=10)
MCP_OPERATOR_AUTH_REFRESH_SKEW = datetime.timedelta(seconds=60)

router = APIRouter(tags=["mcp-operator-oauth"])


def operator_subject_from_idp_tokens(idp_tokens: Mapping[str, Any]) -> str | None:
    """The operator's opaque OIDC subject from an OAuth agent's upstream token response.

    Decodes the upstream ``id_token`` (freshly issued by Authentik in this exchange, so no local
    signature check is needed) and returns its ``sub`` — the same opaque identifier the operator
    browser session and the `mcp_operator_oauth_associations` key use (both console providers run
    ``sub_mode=user_id``, so ``sub`` is the stable Authentik user id, consistent across providers).
    ``None`` when there is no id_token or ``sub`` claim.
    """
    id_token = idp_tokens.get("id_token")
    if not isinstance(id_token, str):
        return None
    claims = jwt.decode(id_token, options={"verify_signature": False, "verify_aud": False})
    subject = claims.get("sub")
    return subject if isinstance(subject, str) else None


class McpOperatorAuthStatusBase(BaseModel):
    server_id: str
    # The operator's human username (preferred_username), for display. The association is keyed
    # internally on the opaque subject; the subject is not exposed in the API.
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
    operator_subject: str
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
            operator_subject=row.operator_subject,
            expires_at=row.expires_at,
            redirect_uri=row.redirect_uri,
            code_verifier=row.code_verifier,
            client_secret_expires_at=row.client_secret_expires_at,
            scope=row.scope,
        )


class OperatorOAuthRefreshState(_OperatorOAuthTokenClient):
    """An `McpOperatorOAuthAssociation` row's refresh inputs read out before its session
    closes, carried across the token-refresh network call."""

    refresh_token: str | None = None

    @classmethod
    def from_row(cls, row: McpOperatorOAuthAssociation) -> OperatorOAuthRefreshState:
        return cls(
            client_id=row.client_id,
            client_secret=row.client_secret,
            token_endpoint_auth_method=row.token_endpoint_auth_method,
            token_endpoint=row.token_endpoint,
            resource=row.resource,
            refresh_token=row.refresh_token,
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

    def __init__(self, database_url: str) -> None:
        # Migrations are applied once at startup (haku.console.database_migrate.apply_migrations), not
        # here — constructing the store neither connects nor mutates schema.
        self._engine = create_engine(database_url, pool_pre_ping=True)
        self._sessions = sessionmaker(self._engine, expire_on_commit=False)

    def list_statuses(
        self, *, servers: list[McpServerEntry], operator_subject: str, username: str
    ) -> McpOperatorAuthStatusResponse:
        oauth_servers = [server for server in servers if _operator_oauth_enabled(server)]
        if not oauth_servers:
            return McpOperatorAuthStatusResponse()
        server_ids = [server.id for server in oauth_servers]
        with self._sessions.begin() as session:
            rows = session.scalars(
                select(McpOperatorOAuthAssociation)
                .where(McpOperatorOAuthAssociation.operator_subject == operator_subject)
                .where(McpOperatorOAuthAssociation.server_id.in_(server_ids))
            ).all()
        by_server = {row.server_id: row for row in rows}
        return McpOperatorAuthStatusResponse(
            associations=[
                _oauth_status_from_row(server.id, username, by_server.get(server.id)) for server in oauth_servers
            ]
        )

    async def connect_flow(
        self, *, server: McpServerEntry, operator_subject: str, public_base_url: str
    ) -> McpOperatorAuthConnectResponse:
        if not _operator_oauth_enabled(server):
            raise HTTPException(status_code=404, detail=f"MCP server {server.id} does not use operator OAuth")
        with self._sessions.begin() as session:
            existing = session.get(McpOperatorOAuthAssociation, (server.id, operator_subject))
            if existing is not None:
                raise HTTPException(
                    status_code=409, detail=f"MCP server {server.id} is already connected; disconnect it first"
                )
        flow = await _build_operator_oauth_flow(server, public_base_url.rstrip("/"))
        with self._sessions.begin() as session:
            now = datetime.datetime.now(datetime.UTC)
            session.execute(delete(McpOperatorOAuthFlow).where(McpOperatorOAuthFlow.expires_at < now))
            session.execute(
                delete(McpOperatorOAuthFlow)
                .where(McpOperatorOAuthFlow.server_id == server.id)
                .where(McpOperatorOAuthFlow.operator_subject == operator_subject)
            )
            session.add(
                McpOperatorOAuthFlow(
                    state=flow.state,
                    server_id=server.id,
                    operator_subject=operator_subject,
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

    async def complete_callback(self, *, state: str, code: str, username: str) -> McpOperatorAuthStatus:
        with self._sessions.begin() as session:
            if (row := session.get(McpOperatorOAuthFlow, state)) is None:
                raise HTTPException(status_code=404, detail="OAuth flow not found or already used")
            session.delete(row)
            flow = OperatorOAuthFlowState.from_row(row)
        now = datetime.datetime.now(datetime.UTC)
        if flow.expires_at < now:
            raise HTTPException(status_code=410, detail="OAuth flow expired; start connection again")
        token = await _exchange_operator_oauth_code(flow, code)
        token_expires_at = _token_expires_at(token, now)
        with self._sessions.begin() as session:
            existing = session.get(
                McpOperatorOAuthAssociation, (flow.server_id, flow.operator_subject), with_for_update=True
            )
            if existing is not None:
                raise HTTPException(
                    status_code=409, detail=f"MCP server {flow.server_id} is already connected; disconnect it first"
                )
            existing = McpOperatorOAuthAssociation(
                server_id=flow.server_id,
                operator_subject=flow.operator_subject,
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
                token_expires_at=token_expires_at,
            )
            session.add(existing)
            return _oauth_status_from_row(flow.server_id, username, existing)

    def disconnect(self, *, server_id: str, operator_subject: str) -> None:
        with self._sessions.begin() as session:
            row = session.get(McpOperatorOAuthAssociation, (server_id, operator_subject), with_for_update=True)
            if row is not None:
                session.delete(row)

    async def access_token_for(self, *, server: McpServerEntry, operator_subject: str) -> str | None:
        if not _operator_oauth_enabled(server):
            return None
        with self._sessions.begin() as session:
            row = session.get(McpOperatorOAuthAssociation, (server.id, operator_subject), with_for_update=True)
            if row is None:
                return None
            now = datetime.datetime.now(datetime.UTC)
            if row.token_expires_at is None or row.token_expires_at > now + MCP_OPERATOR_AUTH_REFRESH_SKEW:
                return row.access_token
            if not row.refresh_token:
                return None
            snapshot = OperatorOAuthRefreshState.from_row(row)
        refreshed = await _refresh_operator_oauth_token(snapshot)
        token_expires_at = _token_expires_at(refreshed, datetime.datetime.now(datetime.UTC))
        with self._sessions.begin() as session:
            row = session.get(McpOperatorOAuthAssociation, (server.id, operator_subject), with_for_update=True)
            if row is None:
                return None
            row.updated_at = datetime.datetime.now(datetime.UTC)
            row.access_token = refreshed.access_token
            row.refresh_token = refreshed.refresh_token or row.refresh_token
            row.token_type = refreshed.token_type
            row.scope = refreshed.scope or row.scope
            row.token_expires_at = token_expires_at
            return row.access_token

    def upsert_agent_operator(self, *, agent_dcr_client_id: str, operator_subject: str) -> None:
        """Record (or update) which operator subject an OAuth agent (keyed by its DCR client_id) acts as."""
        with self._sessions.begin() as session:
            now = datetime.datetime.now(datetime.UTC)
            row = session.get(McpAgentOperator, agent_dcr_client_id, with_for_update=True)
            if row is None:
                session.add(
                    McpAgentOperator(
                        agent_dcr_client_id=agent_dcr_client_id,
                        operator_subject=operator_subject,
                        created_at=now,
                        updated_at=now,
                    )
                )
            else:
                row.operator_subject = operator_subject
                row.updated_at = now

    def agent_operator(self, agent_dcr_client_id: str) -> str | None:
        """The operator subject an OAuth agent is linked to, or None if unlinked."""
        with self._sessions.begin() as session:
            row = session.get(McpAgentOperator, agent_dcr_client_id)
            return row.operator_subject if row is not None else None


def _oauth_store(request: Request) -> PostgresMcpOperatorOAuthStore:
    return cast(PostgresMcpOperatorOAuthStore, request.app.state.mcp_operator_oauth_store)


OAuthStoreDep = Annotated[PostgresMcpOperatorOAuthStore, Depends(_oauth_store)]


def _operator_subject(request: Request) -> str:
    """The current operator's opaque OIDC subject — the key for associations and the agent→operator
    link. An authenticated operator always has one; its absence is an auth error, not a fallback."""
    subject = operator_auth.operator_subject(request)
    if subject is None:
        raise HTTPException(status_code=401, detail="no authenticated operator subject on the request")
    return subject


def _operator_username(request: Request) -> str:
    """The current operator's human username, for display in status responses / callback messages.
    Display only (never a key), so a benign label is fine when the username claim is absent."""
    return operator_auth.operator_username(request) or "operator"


def _public_base_url(request: Request, settings: Settings) -> str:
    if settings.public_base_url:
        return settings.public_base_url.rstrip("/")
    proto = (request.headers.get("x-forwarded-proto") or request.url.scheme).split(",", 1)[0].strip()
    host = (
        (request.headers.get("x-forwarded-host") or request.headers.get("host") or request.url.netloc)
        .split(",", 1)[0]
        .strip()
    )
    return f"{proto}://{host}"


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


def _token_expires_at(token: OAuthToken, now: datetime.datetime) -> datetime.datetime | None:
    if token.expires_in is None:
        return None
    return now + datetime.timedelta(seconds=token.expires_in)


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
    assert server.operator_oauth is not None
    try:
        async with httpx.AsyncClient(follow_redirects=False, timeout=10.0) as client:
            auth_probe = await client.get(server_url, headers=_metadata_request_headers())
            resource_metadata = await _discover_protected_resource(client, server_url, auth_probe)
            oauth_metadata = await _discover_oauth_metadata(client, server_url, resource_metadata)
            scope = (
                " ".join(server.operator_oauth.scopes)
                if server.operator_oauth.scopes is not None
                else get_client_metadata_scopes(
                    extract_scope_from_www_auth(auth_probe), resource_metadata, oauth_metadata
                )
            )
            client_metadata = OAuthClientMetadata(
                client_name=server.operator_oauth.client_name,
                redirect_uris=[redirect_uri],
                grant_types=["authorization_code", "refresh_token"],
                response_types=["code"],
                scope=scope,
            )
            if server.operator_oauth.static_client_id:
                client_info = OAuthClientInformationFull(
                    client_id=server.operator_oauth.static_client_id,
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
    assert server.operator_oauth is not None
    # operator_oauth is meaningless for an in-process server (there's no remote authorization
    # server to run DCR/metadata discovery against); bind server_url to a local for stable
    # mypy narrowing across the awaits.
    assert server.server_url is not None
    server_url = server.server_url
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
    if response.status_code != 200:
        raise HTTPException(status_code=502, detail=f"MCP OAuth token exchange failed: {response.status_code}")
    try:
        return await handle_token_response_scopes(response)
    except ValidationError as e:
        raise HTTPException(status_code=502, detail=f"MCP OAuth token response was invalid: {e}") from e


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
    if response.status_code != 200:
        raise RuntimeError(f"MCP OAuth token refresh failed: {response.status_code}")
    try:
        return await handle_token_response_scopes(response)
    except ValidationError as e:
        raise RuntimeError(f"MCP OAuth refresh response was invalid: {e}") from e


# The callback page is served here (not from the SPA) because the OAuth provider redirects
# straight to this backend endpoint, where the code→token exchange runs; the page's only job
# is to report the outcome. The server publishes association changes through the console event
# hub. The markup lives in a sibling
# .html file (loaded once at import) so it stays lintable rather than a Python blob;
# `string.Template` `$` placeholders avoid colliding with the CSS/JS braces.
# TODO: make this a SPA-style page instead of a backend-served .html template — have the callback
# run the token exchange, then redirect to a frontend route that renders the outcome.
_CALLBACK_TEMPLATE = Template((Path(__file__).parent / "mcp_operator_auth_callback.html").read_text(encoding="utf-8"))


def _oauth_callback_response(ok: bool, message: str, *, status_code: int = 200) -> HTMLResponse:
    title = "MCP account connected" if ok else "MCP account connection failed"
    return HTMLResponse(
        status_code=status_code,
        content=_CALLBACK_TEMPLATE.substitute(title=html.escape(title), message=html.escape(message)),
    )


@router.get("/api/mcp/operator-auth")
async def mcp_operator_auth_statuses(
    request: Request, settings: SettingsDep, oauth_store: OAuthStoreDep
) -> McpOperatorAuthStatusResponse:
    return oauth_store.list_statuses(
        servers=_load_servers(settings),
        operator_subject=_operator_subject(request),
        username=_operator_username(request),
    )


@router.post("/api/mcp/operator-auth/{server_id}/connect")
async def connect_mcp_operator_auth(
    server_id: str, request: Request, csrf_protect: Csrf, settings: SettingsDep, oauth_store: OAuthStoreDep
) -> McpOperatorAuthConnectResponse:
    await csrf_protect.validate_csrf(request)
    server = _server_entry(settings, server_id)
    return await oauth_store.connect_flow(
        server=server, operator_subject=_operator_subject(request), public_base_url=_public_base_url(request, settings)
    )


@router.delete("/api/mcp/operator-auth/{server_id}")
async def disconnect_mcp_operator_auth(
    server_id: str, request: Request, csrf_protect: Csrf, oauth_store: OAuthStoreDep, event_hub: ConsoleEventHubDep
) -> McpOperatorAuthUnconnected:
    await csrf_protect.validate_csrf(request)
    operator_subject = _operator_subject(request)
    oauth_store.disconnect(server_id=server_id, operator_subject=operator_subject)
    await event_hub.broadcast(
        operator_subject, [McpOperatorAuthChangedEvent(server_id=server_id, status="disconnected")]
    )
    return McpOperatorAuthUnconnected(server_id=server_id, username=_operator_username(request))


@router.get(MCP_OPERATOR_AUTH_CALLBACK_PATH)
async def mcp_operator_auth_callback(
    request: Request,
    oauth_store: OAuthStoreDep,
    event_hub: ConsoleEventHubDep,
    state: str | None = None,
    code: str | None = None,
    error: str | None = None,
) -> HTMLResponse:
    if error:
        return _oauth_callback_response(False, f"MCP OAuth authorization failed: {error}", status_code=400)
    if not state or not code:
        return _oauth_callback_response(False, "MCP OAuth callback is missing state or code.", status_code=400)
    try:
        status = await oauth_store.complete_callback(state=state, code=code, username=_operator_username(request))
    except HTTPException as e:
        detail = e.detail if isinstance(e.detail, str) else "MCP OAuth callback failed."
        return _oauth_callback_response(False, detail, status_code=e.status_code)
    await event_hub.broadcast(
        _operator_subject(request), [McpOperatorAuthChangedEvent(server_id=status.server_id, status="connected")]
    )
    return _oauth_callback_response(True, f"Connected {status.server_id} for {_operator_username(request)}.")
