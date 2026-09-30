"""Configuration for the Airlock OAuth credential broker."""

from __future__ import annotations

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from airlock.oauth.config import OAuthConfig
from airlock.oauth.provider import GenericOAuth2Provider


class _ProviderClientCredentials(BaseSettings):
    model_config = SettingsConfigDict(case_sensitive=False)

    client_id: str
    client_secret: SecretStr


def build_oauth_providers(oauth_config: OAuthConfig, default_redirect_uri: str) -> dict[str, GenericOAuth2Provider]:
    """Construct OAuth provider instances from config + env vars.

    `default_redirect_uri` is the shared callback URL used by any provider that does
    not set its own (legacy) `redirect_uri`.
    """
    providers: dict[str, GenericOAuth2Provider] = {}
    for p in oauth_config.providers:
        # Pydantic Settings handles env lookup and field suffixes. Provider IDs allow
        # hyphens, which the existing POSIX environment-variable names spell as underscores.
        credentials = _ProviderClientCredentials(_env_prefix=f"{p.name.replace('-', '_')}_")
        providers[p.name] = GenericOAuth2Provider(
            p, credentials.client_id, credentials.client_secret.get_secret_value(), default_redirect_uri
        )
    return providers
