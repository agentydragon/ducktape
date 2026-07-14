"""Operator **browser** authentication for haku-console (Authentik OIDC).

The console authenticates the operator's browser itself via the Authentik authorization-code flow,
storing the identity in a signed session cookie. Agent access to `/mcp` uses its own MultiAuth
(OIDCProxy DCR + static bearer) and is unaffected.

Two router-level dependency guards enforce the split (applied by `app.py` when it includes each
router, so a new route on a guarded router is protected by default):

- `require_operator` — the operator-only surface (approvals, decisions, account-linking): a valid
  operator session (an OIDC subject) is required;
- `require_operator_or_static_agent` — the agent-facing tool-call routes (submit + read/sweep): an
  operator session OR a configured static agent's bearer.

The static SPA (served by nginx) stays public; on a 401 the frontend redirects to `/auth/login`.
`/mcp` (its own MultiAuth), `/healthz`, and `/auth/*` are not under `/api/` and carry their own auth.
"""

from __future__ import annotations

import hmac
import logging
from typing import Annotated, cast

from authlib.integrations.starlette_client import OAuth, OAuthError
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from starlette.requests import HTTPConnection

from haku.console.config import OperatorOidcConfig, Settings
from haku.console.mcp_config import ResolvedStaticAgent
from haku.console.tool_call_actor import AgentActor, OperatorActor, ToolCallActor

logger = logging.getLogger(__name__)

AUTHENTIK_CLIENT_NAME = "authentik"
SESSION_USER_KEY = "operator"

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
    audit identity (`agent`) and the operator it acts as (`operator_subject`). Both the agent-facing
    router guard (`require_operator_or_static_agent`) and the tool-call caller resolution
    (`mcp_approval`) route through it, so there is a single token→agent→operator mapping."""
    return next((a for a in static_agents if presents_agent_bearer(conn, a.token.get_secret_value())), None)


class OperatorResponse(BaseModel):
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


def operator_from_session(request: Request) -> str | None:
    """The signed-cookie operator username, or None. Sessions are client-side signed, so callers
    that authorize on this must re-check it against the configured operator."""
    if "session" not in request.scope:
        return None
    user = request.session.get(SESSION_USER_KEY)
    return user.get("username") if isinstance(user, dict) else None


def operator_subject(conn: HTTPConnection) -> str | None:
    """The authenticated operator's opaque OIDC subject (Authentik `sub`), or None.

    This is the *key* the `operator_oauth` associations and the agent→operator link use — never the
    mutable username. Sourced **only** from the app-owned OIDC session: there is no `x-authentik-*`
    request-header fallback. Those were the retired forward-auth outpost's headers; with the outpost
    gone and traffic reaching the app directly, any client could forge them, so they are not trusted.
    Takes `HTTPConnection` (the Request/WebSocket base) so the router guards work on the WebSocket
    route too."""
    if "session" in conn.scope:
        user = conn.session.get(SESSION_USER_KEY)
        if isinstance(user, dict) and isinstance(subject := user.get("subject"), str):
            return subject
    return None


def operator_username(request: Request) -> str | None:
    """The authenticated operator's `preferred_username`, or None — for display/audit, never a key.

    Sourced only from the app-owned OIDC session; no `x-authentik-username` request-header fallback
    (see `operator_subject` for why the retired outpost's headers are no longer trusted)."""
    return operator_from_session(request)


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
    # Authorization is Authentik's job (the application access policy gates who reaches here); we only
    # need a valid token. The opaque `sub` is the identity key (associations / agent→operator link);
    # the username is kept only as a display label for audit and the settings UI.
    userinfo = token.get("userinfo") or {}
    subject = userinfo.get("sub")
    username = userinfo.get("preferred_username") or userinfo.get("nickname") or subject
    if not subject or not username:
        raise HTTPException(status_code=401, detail="OIDC token missing sub or username claim")
    request.session[SESSION_USER_KEY] = {"subject": subject, "username": username}
    logger.info("operator browser login: %s (sub=%s)", username, subject)
    return RedirectResponse(url="/", status_code=303)


@router.get("/logout")
async def logout(request: Request) -> RedirectResponse:
    request.session.pop(SESSION_USER_KEY, None)
    return RedirectResponse(url="/", status_code=303)


@router.get("/me")
async def me(request: Request) -> OperatorResponse:
    username = operator_from_session(request)
    if not username:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return OperatorResponse(username=username)


def _static_agents(conn: HTTPConnection) -> list[ResolvedStaticAgent]:
    return cast("list[ResolvedStaticAgent]", conn.app.state.static_agents)


StaticAgentsDep = Annotated[list[ResolvedStaticAgent], Depends(_static_agents)]


def _operator_actor(conn: HTTPConnection) -> OperatorActor:
    if (subject := operator_subject(conn)) is None:
        raise HTTPException(status_code=401, detail="no authenticated operator subject on the request")
    return OperatorActor(operator_subject=subject)


OperatorActorDep = Annotated[OperatorActor, Depends(_operator_actor)]


def _tool_call_actor(conn: HTTPConnection, static_agents: StaticAgentsDep) -> ToolCallActor:
    """Resolve exactly one presented credential into its audit identity and tenant."""
    agent = authenticated_static_agent(conn, static_agents)
    subject = operator_subject(conn)
    if agent is not None and subject is not None:
        raise HTTPException(status_code=400, detail="present exactly one operator or static-agent credential")
    if agent is not None:
        return AgentActor(principal=agent.agent, operator_subject=agent.operator_subject)
    if subject is not None:
        return OperatorActor(operator_subject=subject)
    raise HTTPException(status_code=401, detail="operator or agent authentication required")


ToolCallActorDep = Annotated[ToolCallActor, Depends(_tool_call_actor)]


def require_operator(conn: HTTPConnection) -> None:
    """Router-level guard for the operator-only surface: the caller must present an authenticated
    operator session (an OIDC subject). Applied to the operator routers so a newly added route there
    is protected by default — no path list to keep in sync. Typed on `HTTPConnection` so it guards
    the WebSocket route as well as HTTP routes."""
    if operator_subject(conn) is None:
        raise HTTPException(status_code=401, detail="operator authentication required")


def require_operator_or_static_agent(conn: HTTPConnection) -> None:
    """Router-level guard for the agent-facing tool-call routes (submit + read/sweep): an operator
    session OR a configured static agent's bearer. The operator-only surfaces (approvals, decisions,
    account linking) are never reachable by an agent bearer because they live under `require_operator`."""
    if operator_subject(conn) is None and authenticated_static_agent(conn, _static_agents(conn)) is None:
        raise HTTPException(status_code=401, detail="operator or agent authentication required")
