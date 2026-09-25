"""The app's own OIDC login, so a browser reaches it without a forward-auth proxy in front.

Authentik's application policy binding decides who may log in. PostgreSQL stores the identity,
login tokens and OAuth state; the browser carries only a signed random handle. A login lasts until
it has gone unused for `session_idle_seconds` or reached `session_max_seconds`, whichever is
first; the tokens' own lifetimes do not bound it. Action federation renews the access token with
the refresh token as it needs one (`action_federation.py`).

A login exists iff `AGENTPLANE_OIDC_ISSUER` is set. Unset, a browser has no way in and the only
credential the app accepts is a Kubernetes token (`identity.py`); the API is guarded either way.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime

from authlib.integrations.starlette_client import OAuth
from fastapi import Request
from pydantic import BaseModel, ConfigDict, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from agentplane.app.operator_sessions import OperatorSession, request_session

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
    session_max_seconds: int = Field(
        default=604800, gt=0, description="How long a login lasts at most, however it is used."
    )
    session_idle_seconds: int = Field(
        default=86400, gt=0, description="How long a login lasts without an authenticated request."
    )
    session_activity_step_seconds: int = Field(
        default=300,
        gt=0,
        description="Activity moves the idle deadline only once it would move by more than this, so a "
        "burst of requests does not each rewrite the session row; the idle timeout holds to within it.",
    )
    token_renew_before_seconds: int = Field(
        default=30,
        ge=0,
        description="Renew the login access token once it is this close to expiry, so it cannot lapse "
        "between the check and the provider reading it.",
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
        # offline_access is what makes Authentik issue a refresh token.
        client_kwargs={"scope": "openid profile offline_access", "code_challenge_method": "S256"},
    )
    return oauth


def settings(request: Request) -> OIDCSettings:
    """The app's OIDC settings; only a route or guard that already found a session may ask."""
    configured = request.app.state.oidc
    if not isinstance(configured, OIDCSettings):
        raise TypeError(f"app.state.oidc is {type(configured).__name__}, not OIDCSettings")
    return configured


def session_operator(request: Request) -> str | None:
    """The operator this request's session names, or None when it has none."""
    session = operator_session(request)
    return session.username if session is not None else None


class TokenResponse(BaseModel):
    """The token endpoint's answer, as authlib hands it over with `expires_at` derived from `expires_in`."""

    # Pydantic would otherwise read a Unix time past 2e10 as milliseconds; authlib's is always seconds.
    model_config = ConfigDict(extra="ignore", frozen=True, hide_input_in_errors=True, val_temporal_unit="seconds")

    access_token: SecretStr = Field(min_length=1)
    expires_at: datetime | None = Field(default=None, description="None when the provider stated no lifetime.")
    refresh_token: SecretStr | None = Field(default=None, min_length=1, description="None when it issued none.")


def operator_session(request: Request) -> OperatorSession | None:
    """The login of the session this request presented, which the session middleware only hands on
    unexpired, or None. Its tokens may be stale: the federation path reads them from the row."""
    if request.app.state.oidc is None:
        return None
    return request_session(request).held.login
