"""OIDC (Authentik) login for the props dashboard.

Browser users authenticate against Authentik via the OAuth2 authorization-code
flow; the resulting identity is stored in a signed session cookie. This is a
*parallel* path to the Postgres-credential Bearer/Basic auth in `auth.py`:
machine clients (agents, CI crane pushes, evaluator scripts) keep using tokens,
only humans using the dashboard go through OIDC.

A successful SSO login maps to admin access only (see `auth.get_caller_db`);
evaluator/agent roles remain token-based.

All settings come from `PROPS_OIDC_*` env vars. SSO is enabled iff
`PROPS_OIDC_ISSUER` is set — otherwise the dashboard is token-only (local dev,
tests, the docker-compose stack).
"""

from __future__ import annotations

import os

from authlib.integrations.starlette_client import OAuth
from pydantic_settings import BaseSettings, SettingsConfigDict

ENV_OIDC_ISSUER = "PROPS_OIDC_ISSUER"

# authlib client registration name; referenced by the /auth routes.
AUTHENTIK_CLIENT_NAME = "authentik"


class OIDCSettings(BaseSettings):
    """Authentik OIDC relying-party settings, sourced from PROPS_OIDC_* env vars."""

    model_config = SettingsConfigDict(env_prefix="PROPS_OIDC_", frozen=True)

    # Per-provider issuer, e.g. https://auth.allegedly.works/application/o/props/
    issuer: str
    client_id: str
    client_secret: str
    # Signs the session cookie (HMAC). Generated in Terraform alongside the client.
    session_secret: str
    # Public origin of the dashboard, e.g. https://props.allegedly.works.
    # Used to build the redirect URI and to decide cookie Secure flag.
    public_base_url: str
    # Comma-separated allowlist of emails granted admin via SSO. Defense-in-depth
    # on top of the Authentik application's group policy binding.
    admin_emails: str

    @property
    def redirect_uri(self) -> str:
        return f"{self.public_base_url.rstrip('/')}/auth/callback"

    @property
    def server_metadata_url(self) -> str:
        return f"{self.issuer.rstrip('/')}/.well-known/openid-configuration"

    @property
    def cookie_secure(self) -> bool:
        return self.public_base_url.startswith("https://")

    def is_admin(self, email: str) -> bool:
        allowed = {e.strip().lower() for e in self.admin_emails.split(",") if e.strip()}
        return email.lower() in allowed


def load_oidc_settings() -> OIDCSettings | None:
    """Load OIDC settings from PROPS_OIDC_* env, or None when SSO is not configured."""
    if not os.environ.get(ENV_OIDC_ISSUER):
        return None
    return OIDCSettings()


def build_oauth(settings: OIDCSettings) -> OAuth:
    """Build an authlib OAuth registry with the Authentik provider registered."""
    oauth = OAuth()
    oauth.register(
        name=AUTHENTIK_CLIENT_NAME,
        client_id=settings.client_id,
        client_secret=settings.client_secret,
        server_metadata_url=settings.server_metadata_url,
        client_kwargs={"scope": "openid email profile"},
    )
    return oauth
