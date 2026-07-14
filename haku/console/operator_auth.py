"""Operator **browser** authentication for haku-console (Authentik OIDC).

The console authenticates the operator's browser itself via the Authentik authorization-code flow,
storing the identity in a signed session cookie. Agent access to `/mcp` uses its own MultiAuth
(OIDCProxy DCR + static bearer) and is unaffected.

Two router-level dependency guards enforce the split (applied by `app.py` when it includes each
router, so a new route on a guarded router is protected by default):

- `require_operator` — the operator-only surface (approvals, decisions, account-linking): a valid
  DB-revalidated canonical Operator session is required;
- `require_operator_or_static_agent` — the agent-facing tool-call routes (submit + read/sweep): an
  operator session OR a configured static agent's bearer.

The static SPA (served by nginx) stays public; on a 401 the frontend redirects to `/auth/login`.
`/mcp` (its own MultiAuth), `/healthz`, and `/auth/*` are not under `/api/` and carry their own auth.
"""

from __future__ import annotations

import hmac
import logging
import time
from dataclasses import dataclass
from typing import Annotated, cast
from uuid import UUID

from authlib.integrations.starlette_client import OAuth, OAuthError
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from starlette.requests import HTTPConnection

from haku.console.config import OperatorOidcConfig, Settings
from haku.console.mcp_config import ResolvedStaticAgent
from haku.console.operator_identity import OperatorIdentityError, VerifiedExternalIdentity
from haku.console.operator_identity_store import PostgresOperatorIdentityStore
from haku.console.tool_call_actor import AgentActor, OperatorActor, ToolCallActor

logger = logging.getLogger(__name__)

AUTHENTIK_CLIENT_NAME = "authentik"
SESSION_USER_KEY = "operator"
OPERATOR_SESSION_MAX_AGE_SECONDS = 60 * 60

router = APIRouter(prefix="/auth", tags=["operator-auth"])


def presents_agent_bearer(conn: HTTPConnection, token: str) -> bool:
    """Constant-time check that the connection carries `token` as its Bearer credential."""
    presented = conn.headers.get("authorization", "").encode()
    return hmac.compare_digest(presented, f"Bearer {token}".encode())


def authenticated_static_agent(
    conn: HTTPConnection, static_agents: list[ResolvedStaticAgent]
) -> ResolvedStaticAgent | None:
    """The static agent whose configured bearer this request presents, or None.

    The one place a presented agent token maps to its `ResolvedStaticAgent` — which carries both the
    audit identity (`agent`) and the canonical Operator it acts as (`operator_id`). Both the agent-facing
    router guard (`require_operator_or_static_agent`) and the tool-call caller resolution
    (`mcp_approval`) route through it, so there is a single token→agent→operator mapping."""
    return next((a for a in static_agents if presents_agent_bearer(conn, a.token.get_secret_value())), None)


class OperatorResponse(BaseModel):
    username: str


@dataclass(frozen=True, slots=True)
class OperatorSession:
    operator_id: UUID
    identity_id: UUID
    username: str


def build_oauth(config: OperatorOidcConfig) -> OAuth:
    """Build an authlib OAuth registry with the Authentik provider registered."""
    oauth = OAuth()
    oauth.register(
        name=AUTHENTIK_CLIENT_NAME,
        client_id=config.client_id,
        client_secret=config.client_secret.get_secret_value(),
        server_metadata_url=config.server_metadata_url,
        client_kwargs={"scope": "openid email profile"},
    )
    return oauth


def _identity_store(conn: HTTPConnection) -> PostgresOperatorIdentityStore:
    return cast(PostgresOperatorIdentityStore, conn.app.state.operator_identity_store)


def operator_session(conn: HTTPConnection) -> OperatorSession | None:
    """The DB-revalidated browser session, or ``None`` when malformed, stale, or disabled."""
    if "session" not in conn.scope:
        return None
    raw = conn.session.get(SESSION_USER_KEY)
    if not isinstance(raw, dict):
        return None
    try:
        operator_id = UUID(raw["operator_id"])
        identity_id = UUID(raw["identity_id"])
    except (KeyError, TypeError, ValueError):
        return None
    username = raw.get("username")
    if not isinstance(username, str) or not username:
        return None
    expires_at = raw.get("expires_at")
    if not isinstance(expires_at, int) or isinstance(expires_at, bool) or expires_at <= int(time.time()):
        return None
    identity = _identity_store(conn).resolve_active_session(operator_id=operator_id, identity_id=identity_id)
    if identity is None:
        return None
    return OperatorSession(operator_id=identity.operator_id, identity_id=identity.identity_id, username=username)


def operator_username(request: Request) -> str | None:
    """The authenticated operator's `preferred_username`, or None — for display/audit, never a key.

    Sourced only from a DB-revalidated app-owned OIDC session; no request-header fallback."""
    session = operator_session(request)
    return session.username if session is not None else None


def _redirect_uri(request: Request) -> str:
    settings = cast(Settings, request.app.state.settings)
    base = settings.public_base_url.rstrip("/")
    return f"{base}/auth/callback"


@router.get("/login")
async def login(request: Request) -> RedirectResponse:
    client = cast(OAuth, request.app.state.operator_oauth).create_client(AUTHENTIK_CLIENT_NAME)
    return cast(RedirectResponse, await client.authorize_redirect(request, _redirect_uri(request)))


@router.get("/callback")
async def callback(request: Request) -> RedirectResponse:
    client = cast(OAuth, request.app.state.operator_oauth).create_client(AUTHENTIK_CLIENT_NAME)
    try:
        token = await client.authorize_access_token(request)
    except OAuthError as e:
        raise HTTPException(status_code=401, detail=f"OIDC error: {e.error}")
    # Authlib verifies the authorization response and ID-token/userinfo claims against discovered
    # provider metadata. Pin that verified issuer back to Haku's configured trust input explicitly:
    # discovery at a configured URL must not be able to substitute a different issuer.
    userinfo = token.get("userinfo") or {}
    settings = cast(Settings, request.app.state.settings)
    issuer = userinfo.get("iss")
    if not isinstance(issuer, str) or issuer != settings.operator_oidc.issuer:
        raise HTTPException(status_code=401, detail="OIDC token issuer does not match configured issuer")
    subject = userinfo.get("sub")
    if not isinstance(subject, str) or not subject.strip():
        raise HTTPException(status_code=401, detail="OIDC token missing valid sub claim")
    display_candidates = (userinfo.get("preferred_username"), userinfo.get("nickname"), subject)
    username = next(
        (candidate for candidate in display_candidates if isinstance(candidate, str) and candidate.strip()), None
    )
    if username is None:
        raise HTTPException(status_code=401, detail="OIDC token missing valid username claim")
    try:
        identity = _identity_store(request).resolve_verified_identity(
            VerifiedExternalIdentity(issuer=issuer, subject=subject)
        )
    except OperatorIdentityError as error:
        raise HTTPException(status_code=401, detail="OIDC identity is not authorized") from error
    request.session[SESSION_USER_KEY] = {
        "operator_id": str(identity.operator_id),
        "identity_id": str(identity.identity_id),
        "username": username,
        # SessionMiddleware refreshes its cookie timestamp whenever it serializes the session. Keep
        # an independently signed absolute deadline so an active browser cannot turn the cookie
        # into a sliding authorization that outlives Authentik reauthentication indefinitely.
        "expires_at": int(time.time()) + OPERATOR_SESSION_MAX_AGE_SECONDS,
    }
    logger.info("operator browser login: %s (operator_id=%s)", username, identity.operator_id)
    return RedirectResponse(url="/", status_code=303)


@router.get("/logout")
async def logout(request: Request) -> RedirectResponse:
    request.session.pop(SESSION_USER_KEY, None)
    return RedirectResponse(url="/", status_code=303)


@router.get("/me")
async def me(request: Request) -> OperatorResponse:
    session = operator_session(request)
    if session is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return OperatorResponse(username=session.username)


def _static_agents(conn: HTTPConnection) -> list[ResolvedStaticAgent]:
    return cast("list[ResolvedStaticAgent]", conn.app.state.static_agents)


StaticAgentsDep = Annotated[list[ResolvedStaticAgent], Depends(_static_agents)]


def _operator_actor(conn: HTTPConnection) -> OperatorActor:
    session = operator_session(conn)
    if session is None:
        raise HTTPException(status_code=401, detail="no active authenticated operator on the request")
    return OperatorActor(operator_id=session.operator_id)


OperatorActorDep = Annotated[OperatorActor, Depends(_operator_actor)]


def _tool_call_actor(conn: HTTPConnection, static_agents: StaticAgentsDep) -> ToolCallActor:
    """Resolve exactly one presented credential into its audit identity and tenant."""
    agent = authenticated_static_agent(conn, static_agents)
    session = operator_session(conn)
    if agent is not None and session is not None:
        raise HTTPException(status_code=400, detail="present exactly one operator or static-agent credential")
    if agent is not None:
        if not _identity_store(conn).is_active(agent.operator_id):
            raise HTTPException(status_code=401, detail="static agent's operator is disabled or missing")
        return AgentActor(principal=agent.agent, operator_id=agent.operator_id)
    if session is not None:
        return OperatorActor(operator_id=session.operator_id)
    raise HTTPException(status_code=401, detail="operator or agent authentication required")


ToolCallActorDep = Annotated[ToolCallActor, Depends(_tool_call_actor)]


def require_operator(conn: HTTPConnection) -> None:
    """Router-level guard for the operator-only surface: the caller must present an authenticated
    canonical Operator session. Applied to the operator routers so a newly added route there is
    protected by default — no path list to keep in sync. Typed on `HTTPConnection` so it guards the
    WebSocket route as well as HTTP routes."""
    _operator_actor(conn)


def require_operator_or_static_agent(conn: HTTPConnection) -> None:
    """Router-level guard for the agent-facing tool-call routes (submit + read/sweep): an operator
    session OR a configured static agent's bearer. The operator-only surfaces (approvals, decisions,
    account linking) are never reachable by an agent bearer because they live under `require_operator`."""
    if operator_session(conn) is not None:
        return
    agent = authenticated_static_agent(conn, _static_agents(conn))
    if agent is None or not _identity_store(conn).is_active(agent.operator_id):
        raise HTTPException(status_code=401, detail="operator or agent authentication required")
