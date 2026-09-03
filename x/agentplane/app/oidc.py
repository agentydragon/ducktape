"""The app's own OIDC login, so a browser reaches it without a forward-auth proxy in front.

Authentik's application policy binding still decides who may log in, so there is no allowlist here:
a token this issuer signed is an operator. The session is a signed cookie and nothing more -- no
store, no refresh -- because the only thing the app does with an identity is name it in an egress
approval, and a cookie's own deadline is the revocation the app can honour.

A login exists iff `AGENTPLANE_OIDC_ISSUER` is set. Unset, a browser has no way in and the only
credential the app accepts is a Kubernetes token (`identity.py`); the API is guarded either way.
"""

from __future__ import annotations

import logging
import os

from authlib.integrations.starlette_client import OAuth
from fastapi import Request
from pydantic import Field
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
        default=28800, description="How long a login lasts. There is no refresh; the cookie simply expires."
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
    if "session" not in request.scope:
        return None
    user = request.session.get("user")
    if not isinstance(user, dict):
        return None
    username = user.get("username")
    return username if isinstance(username, str) else None
