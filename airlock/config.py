"""Configuration for the Airlock OAuth credential broker."""

from __future__ import annotations

import os

from airlock.oauth.config import OAuthConfig, client_credentials_env_prefix
from airlock.oauth.provider import GenericOAuth2Provider


def build_oauth_providers(oauth_config: OAuthConfig, default_redirect_uri: str) -> dict[str, GenericOAuth2Provider]:
    """Construct OAuth provider instances from config + env vars.

    `default_redirect_uri` is the shared callback URL used by any provider that does
    not set its own (legacy) `redirect_uri`.
    """
    providers: dict[str, GenericOAuth2Provider] = {}
    for p in oauth_config.providers:
        # `google-write` reads GOOGLE_WRITE_*: a hyphen is not valid in a POSIX env var name.
        prefix = client_credentials_env_prefix(p.name)
        client_id = os.environ[f"{prefix}_CLIENT_ID"]
        client_secret = os.environ[f"{prefix}_CLIENT_SECRET"]
        providers[p.name] = GenericOAuth2Provider(p, client_id, client_secret, default_redirect_uri)
    return providers
