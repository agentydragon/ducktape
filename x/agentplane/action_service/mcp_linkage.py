"""PostgreSQL-backed operator OAuth linkage for configured remote MCP servers."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import logging
import secrets
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from urllib.parse import urlencode
from uuid import UUID, uuid4

import httpx
from mcp.client.auth.utils import (
    build_oauth_authorization_server_metadata_discovery_urls,
    build_protected_resource_metadata_discovery_urls,
    extract_resource_metadata_from_www_auth,
    extract_scope_from_www_auth,
    get_client_metadata_scopes,
    handle_auth_metadata_response,
    handle_protected_resource_response,
)
from mcp.shared.auth import OAuthMetadata, ProtectedResourceMetadata
from mcp.shared.auth_utils import check_resource_allowed, resource_url_from_server_url
from prometheus_client import Histogram
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine

from x.agentplane.action_service.catalog import Key
from x.agentplane.action_service.db import McpLinkageFlowRow, McpOAuthTokenStateRow, McpServerLinkageRow, SessionMaker
from x.agentplane.action_service.models import Principal, PrincipalRole

logger = logging.getLogger(__name__)
_REFRESH_SKEW = timedelta(minutes=1)
_REFRESH_SWEEP_INTERVAL = timedelta(seconds=30)
_REFRESH_CLAIM_TTL = timedelta(seconds=30)
_REFRESH_CLAIM_WAIT = timedelta(milliseconds=100)
_REFRESH_RETRY_BASE = timedelta(seconds=30)
_REFRESH_RETRY_MAX = timedelta(minutes=15)
_REFRESH_ADVISORY_LOCK = 0x4147504D43524546
MCP_OAUTH_TOKEN_REQUEST_DURATION = Histogram(
    "agentplane_mcp_oauth_token_request_duration_seconds",
    "MCP OAuth discovery and token endpoint request duration",
    ["operation", "outcome"],
)


class McpProvider(StrEnum):
    GITHUB = "github"
    KUBERNETES = "kubernetes"


class McpOAuthServer(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    server_id: Key
    provider: McpProvider
    server_url: str = Field(min_length=1)
    authorization_endpoint: str | None = None
    token_endpoint: str | None = None
    client_id: str = Field(min_length=1)
    client_secret_file: Path | None = None
    redirect_uri: str = Field(min_length=1)
    scopes: list[str] = Field(default_factory=list)
    resource: str | None = None


class McpLinkageStart(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    scopes: list[str] = Field(default_factory=list)


class McpLinkageStatus(StrEnum):
    UNLINKED = "unlinked"
    LINKED = "linked"
    DEGRADED = "degraded"
    EXPIRED = "expired"


class McpRefreshFailure(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    action: str
    attempts: int
    retry_at: datetime | None


class McpLinkageView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    server_id: str
    provider: McpProvider
    server_url: str
    status: McpLinkageStatus
    revision: int
    scopes: list[str]
    expires_at: datetime | None
    linked_at: datetime | None
    linked_by: str | None
    refresh_failure: McpRefreshFailure | None = None


class McpLinkageStartView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    flow_id: UUID
    authorization_url: str
    expires_at: datetime


class McpLinkageError(Exception):
    pass


class McpLinkageNotFoundError(McpLinkageError):
    pass


class McpLinkageConflictError(McpLinkageError):
    pass


class _RefreshError(McpLinkageError):
    def __init__(self, message: str, *, action: str) -> None:
        super().__init__(message)
        self.action = action


class McpLinkageAuthority:
    """One shared active token family per configured MCP server, persisted in Postgres."""

    def __init__(
        self,
        sessions: SessionMaker,
        servers: dict[str, McpOAuthServer],
        http: httpx.AsyncClient | None = None,
        engine: AsyncEngine | None = None,
    ) -> None:
        self._sessions = sessions
        self._servers = dict(servers)
        self._http = http
        self._engine = engine
        self._stop = asyncio.Event()
        self._refresh_task: asyncio.Task[None] | None = None
        self._change_events: dict[str, set[asyncio.Event]] = {}

    async def start_refresh_loop(self) -> None:
        if self._refresh_task is None:
            self._stop.clear()
            self._refresh_task = asyncio.create_task(self._refresh_loop(), name="mcp-linkage-refresh")

    async def close(self) -> None:
        if self._refresh_task is None:
            return
        self._stop.set()
        await self._refresh_task
        self._refresh_task = None

    async def cleanup_removed_servers(self) -> None:
        """Delete linkages, token state, and pending flows for removed configuration."""
        if not self._servers:
            return
        configured = set(self._servers)
        async with self._sessions.begin() as db:
            rows = list(await db.scalars(select(McpServerLinkageRow)))
            for row in rows:
                if row.server_id not in configured:
                    if row.token_state_id is not None:
                        token_state = await db.get(McpOAuthTokenStateRow, row.token_state_id)
                        if token_state is not None:
                            await db.delete(token_state)
                    await db.delete(row)
            flows = list(await db.scalars(select(McpLinkageFlowRow)))
            for flow in flows:
                if flow.server_id not in configured:
                    await db.delete(flow)

    def servers(self) -> dict[str, McpOAuthServer]:
        return dict(self._servers)

    def subscribe_changes(self, server_id: str) -> asyncio.Event:
        self._server(server_id)
        changed = asyncio.Event()
        self._change_events.setdefault(server_id, set()).add(changed)
        return changed

    def unsubscribe_changes(self, server_id: str, changed: asyncio.Event) -> None:
        subscribers = self._change_events.get(server_id)
        if subscribers is None:
            return
        subscribers.discard(changed)
        if not subscribers:
            self._change_events.pop(server_id, None)

    def _notify_change(self, server_id: str) -> None:
        for changed in self._change_events.get(server_id, ()):
            changed.set()

    async def statuses(self) -> list[McpLinkageView]:
        return [await self.status(server_id) for server_id in self._servers]

    async def status(self, server_id: str) -> McpLinkageView:
        server = self._server(server_id)
        async with self._sessions() as db:
            row = await db.get(McpServerLinkageRow, server_id)
            state = await db.get(McpOAuthTokenStateRow, row.token_state_id) if row and row.token_state_id else None
        return _view(server, row, state)

    async def start(self, server_id: str, request: McpLinkageStart, operator: Principal) -> McpLinkageStartView:
        self._require_operator(operator)
        server = self._server(server_id)
        authorization_endpoint, token_endpoint, resource, discovered_scopes = await self._discover(server)
        scopes = _scopes(request.scopes or server.scopes or discovered_scopes, server.scopes or discovered_scopes)
        now = datetime.now(UTC)
        expires_at = now + timedelta(minutes=10)
        state = secrets.token_urlsafe(32)
        verifier = secrets.token_urlsafe(48)
        flow_id = uuid4()
        async with self._sessions.begin() as db:
            db.add(
                McpLinkageFlowRow(
                    id=flow_id,
                    server_id=server_id,
                    state_hash=_digest(state),
                    verifier=verifier,
                    operator_principal=operator.key,
                    scopes=scopes,
                    authorization_endpoint=authorization_endpoint,
                    token_endpoint=token_endpoint,
                    resource=resource,
                    expires_at=expires_at,
                    consumed_at=None,
                )
            )
        query = {
            "response_type": "code",
            "client_id": server.client_id,
            "redirect_uri": server.redirect_uri,
            "state": state,
            "code_challenge": _challenge(verifier),
            "code_challenge_method": "S256",
        }
        if scopes:
            query["scope"] = " ".join(scopes)
        if resource:
            query["resource"] = resource
        return McpLinkageStartView(
            flow_id=flow_id, authorization_url=f"{authorization_endpoint}?{urlencode(query)}", expires_at=expires_at
        )

    async def callback(self, state: str, code: str) -> McpLinkageView:
        if not state or not code:
            raise McpLinkageConflictError("OAuth callback is missing state or code")
        async with self._sessions.begin() as db:
            flow = await db.scalar(
                select(McpLinkageFlowRow).where(McpLinkageFlowRow.state_hash == _digest(state)).with_for_update()
            )
            if flow is None or flow.consumed_at is not None or flow.expires_at <= datetime.now(UTC):
                raise McpLinkageConflictError("OAuth linkage flow is invalid or expired")
            server = self._server(flow.server_id)
            verifier = flow.verifier
            operator_principal = flow.operator_principal
            scopes = list(flow.scopes)
            token_endpoint = flow.token_endpoint
            resource = flow.resource
            flow.consumed_at = datetime.now(UTC)
        token = await self._exchange(server, code, verifier, scopes, token_endpoint, resource)
        now = datetime.now(UTC)
        async with self._sessions.begin() as db:
            current = await db.get(McpServerLinkageRow, server.server_id, with_for_update=True)
            revision = (current.revision + 1) if current is not None else 1
            token_state = (
                await db.get(McpOAuthTokenStateRow, current.token_state_id, with_for_update=True)
                if current and current.token_state_id
                else None
            )
            if token_state is None:
                token_state = McpOAuthTokenStateRow(id=uuid4(), server_id=server.server_id)
                db.add(token_state)
            _replace_token_state(token_state, token, scopes, now)
            if current is None:
                current = McpServerLinkageRow(
                    server_id=server.server_id,
                    provider=server.provider.value,
                    server_url=server.server_url,
                    revision=revision,
                    scopes=scopes,
                    token_state_id=token_state.id,
                    token_endpoint=token_endpoint,
                    resource=resource,
                    linked_at=now,
                    linked_by=operator_principal,
                )
                db.add(current)
            else:
                current.provider = server.provider.value
                current.server_url = server.server_url
                current.revision = revision
                current.scopes = scopes
                current.token_state_id = token_state.id
                current.token_endpoint = token_endpoint
                current.resource = resource
                current.linked_at = now
                current.linked_by = operator_principal
            await db.flush()
            view = _view(server, current, token_state)
        self._notify_change(server.server_id)
        return view

    async def disconnect(self, server_id: str, operator: Principal) -> McpLinkageView:
        self._require_operator(operator)
        server = self._server(server_id)
        async with self._sessions.begin() as db:
            row = await db.get(McpServerLinkageRow, server_id, with_for_update=True)
            state = (
                await db.get(McpOAuthTokenStateRow, row.token_state_id, with_for_update=True)
                if row and row.token_state_id
                else None
            )
            if row is not None:
                row.revision += 1
                row.token_state_id = None
                row.scopes = server.scopes
                row.linked_at = None
                row.linked_by = None
            if state is not None:
                await db.delete(state)
            view = _view(server, row, None)
        self._notify_change(server.server_id)
        return view

    async def access_token(self, server_id: str, expected_revision: int) -> str:
        """Resolve a token only after dispatch has fenced the linkage revision."""
        self._server(server_id)
        await self._refresh_if_due(server_id)
        async with self._sessions() as db:
            row = await db.get(McpServerLinkageRow, server_id)
            state = await db.get(McpOAuthTokenStateRow, row.token_state_id) if row and row.token_state_id else None
        if row is None or state is None or row.revision != expected_revision:
            raise McpLinkageConflictError("MCP linkage changed or is not connected")
        if state.expires_at is not None and state.expires_at <= datetime.now(UTC):
            raise McpLinkageConflictError("MCP linkage token has expired; reconnect the server")
        return state.access_token

    async def access_token_for_execution(self, server_id: str) -> str:
        """Resolve the current token at the moment the MCP executor sends a request."""
        await self._refresh_if_due(server_id)
        async with self._sessions() as db:
            linkage = await db.get(McpServerLinkageRow, server_id)
            state = (
                await db.get(McpOAuthTokenStateRow, linkage.token_state_id)
                if linkage and linkage.token_state_id
                else None
            )
        if state is None or (state.expires_at is not None and state.expires_at <= datetime.now(UTC)):
            raise McpLinkageConflictError("MCP server is not linked or its token has expired")
        return state.access_token

    async def _discover(self, server: McpOAuthServer) -> tuple[str, str, str | None, list[str]]:
        """Discover MCP protected-resource and authorization-server metadata before linking."""
        started = asyncio.get_running_loop().time()
        client = self._http or httpx.AsyncClient(timeout=10, follow_redirects=False)
        close = self._http is None
        try:
            probe = await client.get(server.server_url)
            resource_metadata: ProtectedResourceMetadata | None = None
            for url in build_protected_resource_metadata_discovery_urls(
                extract_resource_metadata_from_www_auth(probe), server.server_url
            ):
                resource_candidate = await handle_protected_resource_response(await client.get(url))
                if resource_candidate is not None:
                    resource_metadata = resource_candidate
                    break
            auth_server_url = (
                str(resource_metadata.authorization_servers[0])
                if resource_metadata and resource_metadata.authorization_servers
                else None
            )
            oauth_metadata: OAuthMetadata | None = None
            for url in build_oauth_authorization_server_metadata_discovery_urls(auth_server_url, server.server_url):
                ok, auth_candidate = await handle_auth_metadata_response(await client.get(url))
                if auth_candidate is not None:
                    oauth_metadata = auth_candidate
                    break
                if not ok:
                    break
            authorization_endpoint = (
                str(oauth_metadata.authorization_endpoint)
                if oauth_metadata and oauth_metadata.authorization_endpoint
                else server.authorization_endpoint
            )
            token_endpoint = (
                str(oauth_metadata.token_endpoint)
                if oauth_metadata and oauth_metadata.token_endpoint
                else server.token_endpoint
            )
            if not authorization_endpoint or not token_endpoint:
                raise McpLinkageConflictError("MCP OAuth metadata did not provide authorization and token endpoints")
            configured_resource = (
                str(resource_metadata.resource) if resource_metadata and resource_metadata.resource else None
            )
            requested_resource = resource_url_from_server_url(server.server_url)
            resource = (
                configured_resource
                if configured_resource and check_resource_allowed(requested_resource, configured_resource)
                else server.resource or requested_resource
            )
            scope = (
                " ".join(server.scopes)
                if server.scopes
                else get_client_metadata_scopes(extract_scope_from_www_auth(probe), resource_metadata, oauth_metadata)
            )
            _observe_oauth_metric("discovery", "success", started)
            return authorization_endpoint, token_endpoint, resource, scope.split() if scope else []
        except McpLinkageError:
            _observe_oauth_metric("discovery", "rejected", started)
            raise
        except (httpx.HTTPError, ValueError):
            _observe_oauth_metric("discovery", "transport", started)
            raise McpLinkageConflictError("MCP OAuth metadata discovery failed") from None
        finally:
            if close:
                await client.aclose()

    async def _refresh_loop(self) -> None:
        while not self._stop.is_set():
            with contextlib.suppress(Exception):
                await self._refresh_once()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=_REFRESH_SWEEP_INTERVAL.total_seconds())

    async def _refresh_once(self) -> None:
        if self._engine is None:
            for server_id in self._servers:
                await self._refresh_if_due(server_id)
            return
        async with self._engine.connect() as connection:
            acquired = await connection.scalar(
                text("SELECT pg_try_advisory_lock(:lock)"), {"lock": _REFRESH_ADVISORY_LOCK}
            )
            if not acquired:
                return
            try:
                for server_id in self._servers:
                    await self._refresh_if_due(server_id)
            finally:
                await connection.scalar(text("SELECT pg_advisory_unlock(:lock)"), {"lock": _REFRESH_ADVISORY_LOCK})

    async def _refresh_if_due(self, server_id: str) -> None:
        server = self._server(server_id)
        async with self._sessions.begin() as db:
            linkage = await db.get(McpServerLinkageRow, server_id, with_for_update=True)
            state = (
                await db.get(McpOAuthTokenStateRow, linkage.token_state_id, with_for_update=True)
                if linkage and linkage.token_state_id
                else None
            )
            if linkage is None or state is None or not state.refresh_token:
                return
            now = datetime.now(UTC)
            if state.expires_at is not None and state.expires_at > now + _REFRESH_SKEW:
                return
            if state.refresh_failure_action in {"reconnect", "operator_action"}:
                return
            if state.refresh_retry_at is not None and state.refresh_retry_at > now:
                return
            if state.refresh_claim_expires_at is not None and state.refresh_claim_expires_at > now:
                return
            claim_id = uuid4()
            claim_revision = state.token_revision
            claim_refresh_token = state.refresh_token
            linkage_token_state_id = linkage.token_state_id
            linkage_token_endpoint = linkage.token_endpoint
            linkage_resource = linkage.resource
            state_scope = list(state.scope)
            state.refresh_claim_id = claim_id
            state.refresh_claim_expires_at = now + _REFRESH_CLAIM_TTL
        try:
            refreshed = await self._refresh(
                server, claim_refresh_token, state_scope, linkage_token_endpoint, linkage_resource
            )
        except Exception as error:
            await self._store_refresh_failure(server_id, claim_id, error)
            return
        async with self._sessions.begin() as db:
            current = (
                await db.get(McpOAuthTokenStateRow, linkage_token_state_id, with_for_update=True)
                if linkage_token_state_id
                else None
            )
            if (
                current is None
                or current.refresh_claim_id != claim_id
                or current.token_revision != claim_revision
                or current.refresh_token != claim_refresh_token
            ):
                return
            _replace_token_state(current, refreshed, list(current.scope), datetime.now(UTC))

    async def _store_refresh_failure(self, server_id: str, claim_id: UUID, error: Exception) -> None:
        async with self._sessions.begin() as db:
            linkage = await db.get(McpServerLinkageRow, server_id)
            state = (
                await db.get(McpOAuthTokenStateRow, linkage.token_state_id, with_for_update=True)
                if linkage and linkage.token_state_id
                else None
            )
            if state is None or state.refresh_claim_id != claim_id:
                return
            now = datetime.now(UTC)
            state.refresh_failure_count += 1
            state.refresh_failure_started_at = state.refresh_failure_started_at or now
            state.refresh_failure_latest_at = now
            state.refresh_failure_action = (
                "reconnect" if isinstance(error, _RefreshError) and error.action == "reconnect" else "retrying"
            )
            state.refresh_retry_at = (
                None
                if state.refresh_failure_action == "reconnect"
                else now + min(_REFRESH_RETRY_MAX, _REFRESH_RETRY_BASE * (2 ** min(state.refresh_failure_count - 1, 8)))
            )
            state.refresh_claim_id = None
            state.refresh_claim_expires_at = None

    def _server(self, server_id: str) -> McpOAuthServer:
        try:
            return self._servers[server_id]
        except KeyError:
            raise McpLinkageNotFoundError("unknown MCP server") from None

    @staticmethod
    def _require_operator(operator: Principal) -> None:
        if operator.role is not PrincipalRole.OPERATOR:
            raise McpLinkageError("operator authority is required")

    async def _exchange(
        self,
        server: McpOAuthServer,
        code: str,
        verifier: str,
        scopes: list[str],
        token_endpoint: str,
        resource: str | None,
    ) -> dict[str, object]:
        data = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": server.redirect_uri,
            "client_id": server.client_id,
            "code_verifier": verifier,
        }
        if scopes:
            data["scope"] = " ".join(scopes)
        return await self._post_token(server, token_endpoint, resource, data, operation="exchange")

    async def _refresh(
        self,
        server: McpOAuthServer,
        refresh_token: str,
        scopes: list[str],
        token_endpoint: str | None,
        resource: str | None,
    ) -> dict[str, object]:
        if token_endpoint is None:
            raise _RefreshError("MCP OAuth token endpoint is not configured", action="reconnect")
        data = {"grant_type": "refresh_token", "refresh_token": refresh_token, "client_id": server.client_id}
        if scopes:
            data["scope"] = " ".join(scopes)
        return await self._post_token(server, token_endpoint, resource, data, operation="refresh")

    async def _post_token(
        self, server: McpOAuthServer, token_endpoint: str, resource: str | None, data: dict[str, str], *, operation: str
    ) -> dict[str, object]:
        started = asyncio.get_running_loop().time()
        client_secret = server.client_secret_file.read_text().strip() if server.client_secret_file else None
        if resource:
            data["resource"] = resource
        if client_secret:
            data["client_secret"] = client_secret
        client = self._http or httpx.AsyncClient(timeout=15)
        close = self._http is None
        try:
            response = await client.post(token_endpoint, data=data)
            if response.status_code == 400:
                _observe_oauth_metric(operation, "rejected", started)
                raise _RefreshError("MCP OAuth provider rejected the token", action="reconnect")
            response.raise_for_status()
            body = response.json()
        except _RefreshError:
            raise
        except (httpx.HTTPError, ValueError):
            _observe_oauth_metric(operation, "transport", started)
            raise _RefreshError("MCP OAuth token endpoint unavailable", action="retrying") from None
        finally:
            if close:
                await client.aclose()
        if not isinstance(body, dict) or not isinstance(body.get("access_token"), str):
            _observe_oauth_metric(operation, "invalid_response", started)
            raise _RefreshError("MCP OAuth provider returned no access token", action="reconnect")
        _observe_oauth_metric(operation, "success", started)
        return body


def _observe_oauth_metric(operation: str, outcome: str, started: float) -> None:
    MCP_OAUTH_TOKEN_REQUEST_DURATION.labels(operation=operation, outcome=outcome).observe(
        asyncio.get_running_loop().time() - started
    )


def _replace_token_state(
    state: McpOAuthTokenStateRow, token: dict[str, object], scopes: list[str], now: datetime
) -> None:
    refresh_token = token.get("refresh_token")
    state.access_token = str(token["access_token"])
    if isinstance(refresh_token, str) and refresh_token:
        state.refresh_token = refresh_token
    state.token_type = str(token.get("token_type", "Bearer"))
    state.scope = scopes
    state.expires_at = _expiry(token)
    state.token_revision += 1
    state.updated_at = now
    state.refresh_claim_id = None
    state.refresh_claim_expires_at = None
    state.refresh_failure_count = 0
    state.refresh_failure_started_at = None
    state.refresh_failure_latest_at = None
    state.refresh_failure_action = None
    state.refresh_retry_at = None


def _view(
    server: McpOAuthServer, row: McpServerLinkageRow | None, state: McpOAuthTokenStateRow | None
) -> McpLinkageView:
    if row is None or state is None:
        status = McpLinkageStatus.UNLINKED
        revision = row.revision if row else 0
        scopes = row.scopes if row else server.scopes
        expires_at = linked_at = None
        linked_by = None
        failure = None
    elif state.refresh_failure_action in {"reconnect", "operator_action"}:
        status = McpLinkageStatus.DEGRADED
        revision, scopes, expires_at, linked_at, linked_by = (
            row.revision,
            row.scopes,
            state.expires_at,
            row.linked_at,
            row.linked_by,
        )
        failure = McpRefreshFailure(
            action=state.refresh_failure_action, attempts=state.refresh_failure_count, retry_at=state.refresh_retry_at
        )
    elif state.expires_at is not None and state.expires_at <= datetime.now(UTC):
        status = McpLinkageStatus.EXPIRED
        revision, scopes, expires_at, linked_at, linked_by = (
            row.revision,
            row.scopes,
            state.expires_at,
            row.linked_at,
            row.linked_by,
        )
        failure = None
    else:
        status = McpLinkageStatus.LINKED
        revision, scopes, expires_at, linked_at, linked_by = (
            row.revision,
            row.scopes,
            state.expires_at,
            row.linked_at,
            row.linked_by,
        )
        failure = None
    return McpLinkageView(
        server_id=server.server_id,
        provider=server.provider,
        server_url=server.server_url,
        status=status,
        revision=revision,
        scopes=scopes,
        expires_at=expires_at,
        linked_at=linked_at,
        linked_by=linked_by,
        refresh_failure=failure,
    )


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _challenge(verifier: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()


def _scopes(requested: list[str], allowed: list[str]) -> list[str]:
    if not set(requested) <= set(allowed):
        raise McpLinkageConflictError("requested OAuth scopes are not configured for this MCP server")
    return list(dict.fromkeys(requested))


def _expiry(token: dict[str, object]) -> datetime | None:
    expires_in = token.get("expires_in")
    if isinstance(expires_in, (int, float)):
        return datetime.now(UTC) + timedelta(seconds=float(expires_in))
    return None
