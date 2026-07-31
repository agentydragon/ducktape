"""Operator **browser** authentication for haku-console (Authentik OIDC).

The console authenticates the operator's browser itself via the Authentik authorization-code flow,
storing the identity in a signed session cookie. `/mcp` accepts either that session (with an exact
Origin check) or an Agent credential through MultiAuth's OIDCProxy/static-bearer path.

`require_operator` guards the entire browser API (approvals, decisions, audit history, and account
linking) with a DB-revalidated canonical Operator session.

The static SPA (served by nginx) stays public; on a 401 the frontend redirects to `/auth/login`,
carrying the page it was on as `return_to` so re-authenticating does not lose the operator's place.
Pending logins live in Postgres, not the session cookie — see `operator_login_flow.py`.
`/mcp`, `/healthz`, and `/auth/*` are not under `/api/` and carry their own admission rules.
"""

from __future__ import annotations

import asyncio
import datetime
import logging
import secrets
import time
from dataclasses import dataclass
from typing import Annotated, Any, cast
from urllib.parse import urlencode, urlsplit, urlunsplit
from uuid import UUID

from authlib.integrations.starlette_client import OAuth, OAuthError
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field
from starlette.requests import HTTPConnection

from haku.console.authentik_operator_token import PostgresAuthentikOperatorTokenStore
from haku.console.config import OperatorOidcConfig, Settings
from haku.console.oauth_callback_page import render_oauth_callback_page
from haku.console.operator_identity import OperatorIdentityError, VerifiedExternalIdentity
from haku.console.operator_identity_store import PostgresOperatorIdentityStore
from haku.console.operator_login_flow import (
    FLOW_LIFETIME_SECONDS,
    LOGIN_COOKIE_PATH,
    LoginFlowOAuth,
    PostgresOperatorLoginFlowStore,
    binding_cookie_name,
    new_browser_binding,
)
from haku.console.tool_call_actor import OperatorActor

logger = logging.getLogger(__name__)

AUTHENTIK_CLIENT_NAME = "authentik"
SESSION_USER_KEY = "operator"
OPERATOR_SESSION_MAX_AGE_SECONDS = 60 * 60
_AGENT_ENROLLMENT_PATH_PREFIX = "/auth/agent-enrollment/"
_MAX_RETURN_TO_LENGTH = 2048
# Every top-level prefix the backend serves. A continuation must name a page the operator can be
# returned to, so none of these qualify (`/auth/` has one exception, the enrollment interaction).
_BACKEND_PATH_PREFIXES = ("/api/", "/auth/", "/mcp", "/healthz", "/.well-known/")
# One automatic restart after a stale or superseded attempt, tracked in its own short-lived cookie
# so the marker cannot be lost to (or evict) anything else the browser holds.
LOGIN_RETRY_COOKIE_NAME = "haku_console_login_retry"
_LOGIN_RETRY_COOKIE_MAX_AGE_SECONDS = 120

router = APIRouter(prefix="/auth", tags=["operator-auth"])


class OperatorResponse(BaseModel):
    username: str
    expires_at: datetime.datetime = Field(
        description="Absolute deadline after which this session stops being accepted. The shell "
        "warns shortly before it, so re-authentication is an expected gesture rather than a "
        "background request failure."
    )


@dataclass(frozen=True, slots=True)
class SignedOperatorSession:
    """What the session cookie claims: well-formed, correctly signed, and inside its deadline.

    Distinct from `OperatorSession`, which is that claim *after* the database has confirmed the
    identity is still active. The event socket needs the difference — an expired cookie means
    "re-authenticate", a rejected identity means "this Operator is gone".
    """

    operator_id: UUID
    identity_id: UUID
    username: str
    browser_session_id: str
    expires_at: datetime.datetime


@dataclass(frozen=True, slots=True)
class OperatorSession:
    operator_id: UUID
    identity_id: UUID
    username: str
    browser_session_id: str
    expires_at: datetime.datetime


def build_oauth(
    config: OperatorOidcConfig, *, login_flows: PostgresOperatorLoginFlowStore, offline_access: bool = False
) -> OAuth:
    """Build an authlib OAuth registry with the Authentik provider registered.

    `offline_access` adds that scope so Authentik returns a refresh token — requested only when the
    console persists the operator's token for hostexec (no needless credential otherwise).

    Each pending authorization request lives in `login_flows`, which authlib reaches through the
    registry's cache slot — see `operator_login_flow.py` for why it is not in the session cookie.
    """
    scope = "openid email profile offline_access" if offline_access else "openid email profile"
    oauth = LoginFlowOAuth(cache=login_flows)
    oauth.register(
        name=AUTHENTIK_CLIENT_NAME,
        client_id=config.client_id,
        client_secret=config.client_secret.get_secret_value(),
        server_metadata_url=config.server_metadata_url,
        client_kwargs={"scope": scope},
    )
    return oauth


def _identity_store(conn: HTTPConnection) -> PostgresOperatorIdentityStore:
    return cast(PostgresOperatorIdentityStore, conn.app.state.operator_identity_store)


def _login_flows(conn: HTTPConnection) -> PostgresOperatorLoginFlowStore:
    return cast(PostgresOperatorLoginFlowStore, conn.app.state.operator_login_flows)


def _rejected(conn: HTTPConnection, reason: str, **detail: object) -> None:
    """Record why an operator session was refused, then yield the ``None`` callers expect.

    Without this, every refusal reaches the browser as one indistinguishable 401 — an expired
    absolute deadline, a cookie the browser never sent, and a disabled identity all look identical,
    which is exactly the ambiguity that made a failed MCP account reconnect undiagnosable. Logs the
    reason and the route only: never the cookie payload, a token, or an OAuth callback parameter.
    """
    logger.info(
        "operator session rejected: reason=%s method=%s path=%s%s",
        reason,
        conn.scope.get("method", "-"),
        conn.url.path,
        "".join(f" {key}={value}" for key, value in detail.items()),
    )


def signed_operator_session(conn: HTTPConnection) -> SignedOperatorSession | None:
    """The cookie's operator payload, or ``None`` when absent, malformed, or past its deadline.

    Pure cookie inspection — no database. Callers that need live authority use `operator_session`.
    Each refusal is logged with its distinguishing reason; see `_rejected`.
    """
    if "session" not in conn.scope:
        return _rejected(conn, "no_session_middleware")
    raw = conn.session.get(SESSION_USER_KEY)
    if not isinstance(raw, dict):
        return _rejected(conn, "no_session_cookie")
    try:
        operator_id = UUID(raw["operator_id"])
        identity_id = UUID(raw["identity_id"])
    except (KeyError, TypeError, ValueError):
        return _rejected(conn, "malformed_identity")
    username = raw.get("username")
    if not isinstance(username, str) or not username:
        return _rejected(conn, "malformed_username")
    browser_session_id = raw.get("browser_session_id")
    if not isinstance(browser_session_id, str) or not browser_session_id:
        return _rejected(conn, "malformed_browser_session")
    expires_at = raw.get("expires_at")
    if not isinstance(expires_at, int) or isinstance(expires_at, bool):
        return _rejected(conn, "malformed_expiry")
    # Split from the type check above so an expired session is never reported as a malformed one:
    # the absolute deadline never slides, so "expired" is an ordinary outcome for a long-lived tab
    # and `expired_for` says immediately whether the deadline is what refused the request.
    now = int(time.time())
    if expires_at <= now:
        return _rejected(conn, "expired", expired_for=datetime.timedelta(seconds=now - expires_at))
    return SignedOperatorSession(
        operator_id=operator_id,
        identity_id=identity_id,
        username=username,
        browser_session_id=browser_session_id,
        expires_at=datetime.datetime.fromtimestamp(expires_at, tz=datetime.UTC),
    )


def operator_session(
    conn: HTTPConnection, *, identity_store: PostgresOperatorIdentityStore | None = None
) -> OperatorSession | None:
    """The DB-revalidated browser session, or ``None`` when malformed, stale, or disabled."""
    signed = signed_operator_session(conn)
    if signed is None:
        return None  # already logged with its distinguishing reason
    store = identity_store if identity_store is not None else _identity_store(conn)
    identity = store.resolve_active_session(operator_id=signed.operator_id, identity_id=signed.identity_id)
    if identity is None:
        return _rejected(conn, "identity_inactive", operator_id=signed.operator_id)
    return OperatorSession(
        operator_id=identity.operator_id,
        identity_id=identity.identity_id,
        username=signed.username,
        browser_session_id=signed.browser_session_id,
        expires_at=signed.expires_at,
    )


def operator_username(request: Request) -> str | None:
    """The authenticated operator's `preferred_username`, or None — for display/audit, never a key.

    Sourced only from a DB-revalidated app-owned OIDC session; no request-header fallback."""
    session = operator_session(request)
    return session.username if session is not None else None


def exact_operator_origin(conn: HTTPConnection) -> bool:
    """Whether a browser connection presents the console's canonical exact Origin."""
    settings = cast(Settings, conn.app.state.settings)
    return conn.headers.get("origin") == settings.public_base_url.rstrip("/")


def require_operator_mutation_origin(conn: HTTPConnection) -> None:
    """Reject unsafe operator-browser requests outside the trusted console shell."""
    if conn.scope["type"] != "http" or conn.scope.get("method") in {"GET", "HEAD", "OPTIONS"}:
        return
    if not exact_operator_origin(conn):
        raise HTTPException(status_code=403, detail="operator mutations require the console's exact Origin")


def _redirect_uri(request: Request) -> str:
    settings = cast(Settings, request.app.state.settings)
    base = settings.public_base_url.rstrip("/")
    return f"{base}/auth/callback"


def _is_enrollment_interaction_path(path: str) -> bool:
    if not path.startswith(_AGENT_ENROLLMENT_PATH_PREFIX):
        return False
    try:
        UUID(path.removeprefix(_AGENT_ENROLLMENT_PATH_PREFIX))
    except ValueError:
        return False
    return True


def _validated_return_to(value: str) -> str:
    """Accept a local console page to continue to, never a general redirect target.

    Two shapes qualify: the agent-enrollment interaction the enrollment entry point sends the
    browser through, and any SPA path — so a tab that re-authenticates comes back where it was
    instead of at the root. Backslashes are refused outright because browsers normalize them to
    slashes, which would turn `/\\evil.example` into a protocol-relative URL after this check.
    """
    if len(value) > _MAX_RETURN_TO_LENGTH or any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise HTTPException(status_code=400, detail="invalid operator login continuation")
    if "\\" in value:
        raise HTTPException(status_code=400, detail="invalid operator login continuation")
    parsed = urlsplit(value)
    if parsed.scheme or parsed.netloc or parsed.fragment or not parsed.path.startswith("/"):
        raise HTTPException(status_code=400, detail="invalid operator login continuation")
    if parsed.path.startswith(_BACKEND_PATH_PREFIXES) and not _is_enrollment_interaction_path(parsed.path):
        raise HTTPException(status_code=400, detail="invalid operator login continuation")
    return urlunsplit(("", "", parsed.path, parsed.query, ""))


def _secure_cookies(request: Request) -> bool:
    settings = cast(Settings, request.app.state.settings)
    return settings.public_base_url.startswith("https://")


@router.get("/login")
async def login(request: Request, return_to: str | None = None) -> Response:
    # One flow row per attempt, with its own binding cookie. Nothing about a pending login touches
    # the session cookie, so any number of console tabs can be mid-login at once.
    state, nonce = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    browser_binding = new_browser_binding()
    redirect_uri = _redirect_uri(request)
    await asyncio.to_thread(
        _login_flows(request).start,
        state=state,
        browser_binding=browser_binding,
        return_to=None if return_to is None else _validated_return_to(return_to),
        data={"redirect_uri": redirect_uri, "nonce": nonce},
    )
    client = cast(OAuth, request.app.state.operator_oauth).create_client(AUTHENTIK_CLIENT_NAME)
    response = cast(RedirectResponse, await client.authorize_redirect(request, redirect_uri, state=state, nonce=nonce))
    response.set_cookie(
        binding_cookie_name(state),
        browser_binding,
        max_age=FLOW_LIFETIME_SECONDS,
        path=LOGIN_COOKIE_PATH,
        httponly=True,
        samesite="lax",
        secure=_secure_cookies(request),
    )
    return response


def _login_failed(request: Request, message: str, *, retry: bool, return_to: str | None) -> Response:
    """A stale or superseded attempt is the ordinary case, not an error the operator should have to
    click through, so `retry` restarts the login once — bounded by a marker cookie. Callers pass
    `retry=False` for the outcomes a restart cannot fix, so neither can loop.
    """
    if retry and request.cookies.get(LOGIN_RETRY_COOKIE_NAME) is None:
        query = f"?{urlencode({'return_to': return_to})}" if return_to is not None else ""
        restarted = RedirectResponse(url=f"/auth/login{query}", status_code=303)
        restarted.set_cookie(
            LOGIN_RETRY_COOKIE_NAME,
            "1",
            max_age=_LOGIN_RETRY_COOKIE_MAX_AGE_SECONDS,
            path=LOGIN_COOKIE_PATH,
            httponly=True,
            samesite="lax",
            secure=_secure_cookies(request),
        )
        return restarted
    failed = render_oauth_callback_page(
        "Operator login failed", message, status_code=401, action_url="/auth/login", action_label="Retry login"
    )
    failed.delete_cookie(LOGIN_RETRY_COOKIE_NAME, path=LOGIN_COOKIE_PATH)
    return failed


@router.get("/callback")
async def callback(request: Request) -> Response:
    flows = _login_flows(request)
    state = request.query_params.get("state")
    pending = await asyncio.to_thread(flows.pending_login, state) if state is not None else None
    if (
        state is not None
        and pending is not None
        and not pending.started_by(request.cookies.get(binding_cookie_name(state)))
    ):
        # RFC 6749 §10.12: the browser finishing an authorization must be the one that started it.
        # Restarting cannot help — either this browser never held the flow, or it is not keeping
        # cookies at all, and a fresh attempt would land here again.
        await asyncio.to_thread(flows.discard, state)
        logger.info("operator browser login rejected: the flow was not started by this browser")
        return _login_failed(
            request,
            "This login attempt was started in a different browser, or this browser is not keeping cookies.",
            retry=False,
            return_to=None,
        )
    client = cast(OAuth, request.app.state.operator_oauth).create_client(AUTHENTIK_CLIENT_NAME)
    try:
        token = await client.authorize_access_token(request)
    except OAuthError as e:
        logger.info("operator browser login failed: %s", e.error)
        message = (
            "This login attempt expired or was superseded by a newer attempt."
            if e.error == "mismatching_state"
            else f"The identity provider rejected this login attempt ({e.error})."
        )
        return _login_failed(request, message, retry=True, return_to=pending.return_to if pending is not None else None)
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
        # Enrollment interactions bind to this random browser session, not merely to possession of
        # a server-generated form nonce or to the Operator identity shared across browser devices.
        "browser_session_id": secrets.token_urlsafe(32),
        # An absolute deadline signed into the payload itself, so the authorization cannot outlive
        # Authentik reauthentication however the cookie's own lifetime is managed. `/auth/me`
        # reports it, so the shell can warn instead of letting a background request fail.
        "expires_at": int(time.time()) + OPERATOR_SESSION_MAX_AGE_SECONDS,
    }
    # Persist the operator's own Authentik token for hostexec (offline_access grants a refresh
    # token). Only when hostexec is configured — otherwise there is no reader for this credential.
    # hostexec lives in the console config file, resolved to this flag at create_app.
    if request.app.state.hostexec_enabled:
        _persist_operator_authentik_token(request, identity.operator_id, token)
    logger.info("operator browser login: %s (operator_id=%s)", username, identity.operator_id)
    # The continuation rides the flow, not the session: it is this attempt's destination, so a
    # second tab logging in cannot redirect the first one somewhere it never asked to go.
    return_to = pending.return_to if pending is not None and pending.return_to is not None else "/"
    response = RedirectResponse(url=return_to, status_code=303)
    if state is not None:
        response.delete_cookie(binding_cookie_name(state), path=LOGIN_COOKIE_PATH)
    if request.cookies.get(LOGIN_RETRY_COOKIE_NAME) is not None:
        response.delete_cookie(LOGIN_RETRY_COOKIE_NAME, path=LOGIN_COOKIE_PATH)
    return response


def _persist_operator_authentik_token(request: Request, operator_id: UUID, token: dict[str, Any]) -> None:
    """Best-effort: store the operator's Authentik token for hostexec. A failure never breaks login
    (hostexec just won't have a token); the exception is logged."""
    store = cast(PostgresAuthentikOperatorTokenStore, request.app.state.authentik_operator_token_store)
    access_token = token.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        logger.warning("operator login: Authentik token had no access_token; hostexec will be unavailable")
        return
    expires_at_ts = token.get("expires_at")
    expires_at = (
        datetime.datetime.fromtimestamp(expires_at_ts, tz=datetime.UTC)
        if isinstance(expires_at_ts, (int, float)) and not isinstance(expires_at_ts, bool)
        else None
    )
    try:
        store.store_login_token(
            operator_id=operator_id,
            access_token=access_token,
            refresh_token=token.get("refresh_token"),
            token_type=token.get("token_type") or "Bearer",
            scope=token.get("scope"),
            expires_at=expires_at,
        )
    except Exception:
        logger.warning("operator login: failed to persist Authentik token for hostexec", exc_info=True)


@router.post("/logout", dependencies=[Depends(require_operator_mutation_origin)])
async def logout(request: Request) -> RedirectResponse:
    request.session.clear()
    return RedirectResponse(url="/", status_code=303)


@router.get("/me")
async def me(request: Request) -> OperatorResponse:
    session = operator_session(request)
    if session is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return OperatorResponse(username=session.username, expires_at=session.expires_at)


def _operator_actor(conn: HTTPConnection) -> OperatorActor:
    session = operator_session(conn)
    if session is None:
        raise HTTPException(status_code=401, detail="no active authenticated operator on the request")
    return OperatorActor(operator_id=session.operator_id)


OperatorActorDep = Annotated[OperatorActor, Depends(_operator_actor)]


def require_operator(actor: OperatorActorDep) -> None:
    """Router-level guard for the operator-only surface: the caller must present an authenticated
    canonical Operator session. Applied to the operator routers so a newly added route there is
    protected by default — no path list to keep in sync. FastAPI caches the same actor dependency
    for the guard and route handler, so identity is resolved exactly once per request."""
