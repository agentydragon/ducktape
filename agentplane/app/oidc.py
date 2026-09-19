"""The app's own OIDC login, so a browser reaches it without a forward-auth proxy in front.

Authentik's application policy binding decides who may log in. PostgreSQL stores the identity,
access token and OAuth state; the browser carries only a signed random handle. Expiry is absolute,
bounded by the ID token and (when retained) access token; renewal requires a fresh login.

A login exists iff `AGENTPLANE_OIDC_ISSUER` is set. Unset, a browser has no way in and the only
credential the app accepts is a Kubernetes token (`identity.py`); the API is guarded either way.
"""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime

from authlib.integrations.starlette_client import OAuth
from fastapi import Request
from pydantic import BaseModel, ConfigDict, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)

ENV_ISSUER = "AGENTPLANE_OIDC_ISSUER"

# The authlib client registration name; the /auth routes look it up by this.
CLIENT_NAME = "authentik"

# The __Host- prefix binds the cookie to this exact origin with no Domain and Path=/, which a
# subdomain cannot then overwrite. It also forces Secure, so it is dropped when SSO runs over http.
SECURE_COOKIE = "__Host-agentplane_session"
INSECURE_COOKIE = "agentplane_session"


class OIDCSettings(BaseSettings):
    """Relying-party settings, from AGENTPLANE_OIDC_* environment variables."""

    model_config = SettingsConfigDict(env_prefix="AGENTPLANE_OIDC_", frozen=True)

    issuer: str = Field(description="Per-provider issuer, e.g. https://auth.example/application/o/agentplane/.")
    client_id: str
    client_secret: str
    session_secret: str = Field(description="Signs the session cookie; minted with the client in Terraform.")
    public_base_url: str = Field(description="The app's public origin, which the redirect URI is built from.")
    session_seconds: int = Field(
        default=28800,
        gt=0,
        description="Maximum absolute login lifetime; token expiry may shorten it. Re-login to renew.",
    )

    @property
    def redirect_uri(self) -> str:
        return f"{self.public_base_url.rstrip('/')}/auth/callback"

    @property
    def server_metadata_url(self) -> str:
        return f"{self.issuer.rstrip('/')}/.well-known/openid-configuration"

    @property
    def secure(self) -> bool:
        return self.public_base_url.startswith("https://")

    @property
    def cookie_name(self) -> str:
        return SECURE_COOKIE if self.secure else INSECURE_COOKIE


def load_settings() -> OIDCSettings | None:
    """The OIDC settings, or None when the app is to run without a login."""
    return OIDCSettings() if os.environ.get(ENV_ISSUER) else None


def build_oauth(settings: OIDCSettings) -> OAuth:
    """An authlib registry holding the one Authentik client, with PKCE on."""
    oauth = OAuth()
    oauth.register(
        name=CLIENT_NAME,
        client_id=settings.client_id,
        client_secret=settings.client_secret,
        server_metadata_url=settings.server_metadata_url,
        client_kwargs={"scope": "openid profile", "code_challenge_method": "S256"},
    )
    return oauth


def settings(request: Request) -> OIDCSettings:
    """The app's OIDC settings; only a route or guard that already found a session may ask."""
    configured = request.app.state.oidc
    if not isinstance(configured, OIDCSettings):
        raise TypeError(f"app.state.oidc is {type(configured).__name__}, not OIDCSettings")
    return configured


def session_operator(request: Request) -> str | None:
    """The operator this request's session names, or None when it has none or it has expired."""
    session = operator_session(request)
    return session.username if session is not None else None


class OperatorSession(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    issuer: str
    subject: str
    username: str
    access_token: SecretStr | None = None
    expires_at: float


def operator_session(request: Request) -> OperatorSession | None:
    if "session" not in request.scope or not request.session.get("user"):
        return None
    session = OperatorSession.model_validate(request.session["user"])
    if session.expires_at <= datetime.now(UTC).timestamp():
        return None
    return session
