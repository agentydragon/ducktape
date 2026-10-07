"""Authentik OIDC browser login for an app whose every page and API sits behind one signed-cookie session.

Authorization Code + PKCE against a confidential client. The browser keeps only a signed cookie with identity and
expiry, never Authentik tokens. `install_login` adds the session, the `/auth` routes and the middleware that sends
a browser without a session to the login and refuses an API call without one. Authentik's application policy
decides who may sign in; `LoginConfig.allowed_subject` narrows that to one `sub` for an app with a single owner.

Other Authentik logins stay out, each held there by something this flow would have to change to admit it:

- `airlock/auth.py` takes only `valid_claims`, `build_oauth` and `CLIENT_NAME`. Its SPA shell (`/` and
  `/static/frontend/*`) loads without a session so it can show `/?auth=failed`; the `require_operator_session`
  dependency guards per route, hands `/oauth/*` the operator's `sub`, and answers 401 where this middleware would
  redirect the upstream provider callback to the login. A failed sign-in and a logout are 303s into the shell, not
  this module's 401 page and `/auth/signed-out`, and its log lines differ. Sharing the router and guard would take a
  failure response, a logout target, log messages and a guard mode that every consumer must supply.
- `haku/console/identity/operator_{auth,login_flow}.py` keeps each pending login in a Postgres row with its own
  binding cookie and `return_to` continuation, requests no PKCE, and trusts the session only after
  `PostgresOperatorIdentityStore` re-resolves the identity on every request.
- `agentplane/app/{oidc,auth_routes}.py` requests `offline_access` and keeps the login tokens in Postgres-backed
  sessions, and pins a single audience where `valid_claims` also admits several audiences whose `azp` is this client.
"""

import json
import logging
import math
import time
from collections.abc import Set
from dataclasses import dataclass
from typing import Any, cast

import httpx
from authlib.integrations.starlette_client import OAuth, OAuthError
from fastapi import APIRouter, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from joserfc.errors import JoseError
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError
from starlette import status
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.middleware.sessions import SessionMiddleware
from starlette.responses import Response

# SessionMiddleware imports itsdangerous lazily; Gazelle cannot see the runtime dependency.
# gazelle:include_dep @pypi//itsdangerous

logger = logging.getLogger(__name__)

CLIENT_NAME = "authentik"
_LOGIN_FLOW_PATHS = {"/auth/login", "/auth/callback", "/auth/signed-out"}


@dataclass(frozen=True)
class LoginConfig:
    """What an app supplies, built from its own settings."""

    issuer: str
    client_id: str
    client_secret: SecretStr
    session_secret: SecretStr
    session_seconds: int
    public_base_url: str
    # The bare cookie name; `session_cookie_name` prefixes `__Host-` over https.
    cookie_name: str
    # Reachable without a session, beside the login flow's own paths.
    public_paths: Set[str]
    signed_out_text: str
    # Logged at info when a session is created; None logs nothing.
    session_created_log: str | None
    # The one `sub` admitted; None leaves admission to Authentik's application policy alone.
    allowed_subject: str | None
    # Routes with their own required authentication dependency (for example, bearer or signed session).
    auth_handled_paths: Set[str] = frozenset()

    @property
    def https(self) -> bool:
        return self.public_base_url.startswith("https://")

    @property
    def session_cookie_name(self) -> str:
        """A host-only Secure cookie in production, a plain name over local HTTP."""
        return f"__Host-{self.cookie_name}" if self.https else self.cookie_name


class LoginSession(BaseModel):
    """The minimum identity data retained in the signed browser session."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    issuer: str
    subject: str = Field(min_length=1)
    username: str = Field(min_length=1)
    expires_at: float = Field(gt=0, allow_inf_nan=False)


def build_oauth(*, issuer: str, client_id: str, client_secret: SecretStr) -> OAuth:
    """A confidential OIDC client with Authorization Code + PKCE, registered as `CLIENT_NAME`."""
    oauth = OAuth()
    oauth.register(
        name=CLIENT_NAME,
        client_id=client_id,
        client_secret=client_secret.get_secret_value(),
        server_metadata_url=f"{issuer.rstrip('/')}/.well-known/openid-configuration",
        client_kwargs={"scope": "openid profile email", "code_challenge_method": "S256"},
    )
    return oauth


def valid_claims(claims: Any, *, issuer: str, client_id: str) -> bool:
    """Whether the ID token's claims name this issuer and client, are unexpired, and identify a user."""
    if not isinstance(claims, dict) or claims.get("iss") != issuer:
        return False

    aud = claims.get("aud")
    if isinstance(aud, str):
        audiences = {aud}
    elif isinstance(aud, list) and all(isinstance(value, str) for value in aud):
        audiences = set(aud)
    else:
        return False

    azp = claims.get("azp")
    if client_id not in audiences or (azp is not None and azp != client_id):
        return False
    if len(audiences) > 1 and azp != client_id:
        return False

    expiry = claims.get("exp")
    if isinstance(expiry, bool) or not isinstance(expiry, (float, int)) or not math.isfinite(expiry):
        return False
    if expiry <= time.time():
        return False

    return all(isinstance(claims.get(name), str) and claims[name] for name in ("sub", "preferred_username"))


def _sign_in_failed(message: str) -> HTMLResponse:
    return HTMLResponse(f'<main><p>{message}</p><a href="/auth/login">Try again</a></main>', status_code=401)


def _login_router(config: LoginConfig) -> APIRouter:
    router = APIRouter(prefix="/auth", tags=["auth"], include_in_schema=False)

    def client(request: Request) -> Any:
        return cast(OAuth, request.app.state.oauth).create_client(CLIENT_NAME)

    @router.get("/login")
    async def login(request: Request) -> RedirectResponse:
        redirect_uri = f"{config.public_base_url}/auth/callback"
        return cast(RedirectResponse, await client(request).authorize_redirect(request, redirect_uri))

    @router.get("/callback")
    async def callback(request: Request) -> Response:
        try:
            token = await client(request).authorize_access_token(request)
        except OAuthError, JoseError, json.JSONDecodeError, httpx.HTTPError:
            # Provider responses and query parameters are caller-controlled: nothing of them is reflected.
            logger.warning("Authentik sign-in failed")
            return _sign_in_failed("Authentik sign-in failed.")

        claims: Any = token.get("userinfo") or {}
        if not valid_claims(claims, issuer=config.issuer, client_id=config.client_id):
            logger.warning("Authentik sign-in returned invalid identity claims")
            return _sign_in_failed("Authentik sign-in failed.")
        if config.allowed_subject is not None and claims["sub"] != config.allowed_subject:
            logger.warning("Authentik sign-in by a subject that is not the owner")
            return _sign_in_failed("This page is for its owner only.")

        request.session.clear()
        # Keep only the identity and expiry; never store Authentik tokens in the browser cookie.
        request.session["user"] = LoginSession(
            issuer=config.issuer,
            subject=claims["sub"],
            username=claims["preferred_username"],
            expires_at=min(time.time() + config.session_seconds, float(claims["exp"])),
        ).model_dump()
        if config.session_created_log is not None:
            logger.info(config.session_created_log)
        return RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)

    @router.post("/logout")
    async def logout(request: Request) -> RedirectResponse:
        if request.headers.get("origin") != config.public_base_url:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Logout requires a same-origin request")
        request.session.clear()
        return RedirectResponse(url="/auth/signed-out", status_code=status.HTTP_303_SEE_OTHER)

    @router.get("/signed-out", response_class=HTMLResponse)
    async def signed_out() -> str:
        return f'<main><p>{config.signed_out_text}</p><a href="/auth/login">Sign in</a></main>'

    return router


def current_session_is_valid(request: Request, config: LoginConfig) -> bool:
    payload = request.session.get("user")
    if payload is None:
        return False
    try:
        session = LoginSession.model_validate(payload)
    except ValidationError:
        request.session.clear()
        return False
    if (
        session.issuer != config.issuer
        or (config.allowed_subject is not None and session.subject != config.allowed_subject)
        or session.expires_at <= time.time()
    ):
        request.session.clear()
        return False
    return True


class RequireLoginMiddleware(BaseHTTPMiddleware):
    """Protect every route but the login flow and the app's public paths, and refuse cross-origin writes to the API."""

    def __init__(self, app: Any, *, config: LoginConfig) -> None:
        super().__init__(app)
        self.config = config

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        if request.url.path in _LOGIN_FLOW_PATHS or request.url.path in self.config.public_paths:
            return await call_next(request)
        if (
            request.url.path.startswith("/api/")
            and request.method not in {"GET", "HEAD", "OPTIONS"}
            and request.headers.get("origin") != self.config.public_base_url
        ):
            return JSONResponse({"detail": "Request origin is not allowed"}, status_code=status.HTTP_403_FORBIDDEN)
        if request.url.path in self.config.auth_handled_paths:
            return await call_next(request)
        if current_session_is_valid(request, self.config):
            return await call_next(request)
        if request.url.path.startswith("/api/") or request.method not in {"GET", "HEAD"}:
            return JSONResponse({"detail": "Not authenticated"}, status_code=status.HTTP_401_UNAUTHORIZED)
        return RedirectResponse(url="/auth/login", status_code=status.HTTP_303_SEE_OTHER)


def install_login(app: FastAPI, config: LoginConfig) -> None:
    """Install signed sessions outside the login check and register the login routes."""
    app.state.oauth = build_oauth(issuer=config.issuer, client_id=config.client_id, client_secret=config.client_secret)
    # Starlette puts the most recently added middleware outermost, so SessionMiddleware goes on after the
    # login check and decodes the cookie first.
    app.add_middleware(RequireLoginMiddleware, config=config)
    app.add_middleware(
        SessionMiddleware,
        secret_key=config.session_secret.get_secret_value(),
        session_cookie=config.session_cookie_name,
        max_age=config.session_seconds,
        same_site="lax",
        https_only=config.https,
        path="/",
    )
    app.include_router(_login_router(config))
