"""Validated, transport-independent OAuth provider configuration."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class TokenSecretConfig(BaseModel):
    name: str


class BaseProviderConfig(BaseModel):
    name: str = Field(description="Provider identifier used in URL paths and env var prefixes")
    display_name: str = Field(description="Human-readable provider name for the UI")
    redirect_uri: str | None = Field(
        default=None,
        description="Legacy per-provider redirect URI. Omit to use the shared "
        "{public_base_url}/oauth/callback (the provider is resolved from OAuth state).",
    )
    refresh_secret: TokenSecretConfig = Field(description="Secret holding all token fields including refresh_token")
    access_secret: TokenSecretConfig = Field(description="Secret holding access_token, token_type, expires_at, scope")


class OAuth2ProviderConfig(BaseProviderConfig):
    provider_type: Literal["oauth2"] = "oauth2"
    authorize_url: str = Field(description="OAuth2 authorization endpoint")
    token_url: str = Field(description="OAuth2 token endpoint")
    scopes: list[str] = Field(description="OAuth2 scopes to request")
    refresh_margin_seconds: int = Field(default=3600, description="Seconds before expiry to trigger refresh")
    extra_auth_params: dict[str, str] = Field(default_factory=dict, description="Extra query params for authorize URL")
    use_pkce: bool = Field(default=False, description="Use PKCE (RFC 7636 S256). Required for SMART on FHIR.")
    aud: str | None = Field(
        default=None,
        description="Optional `aud` param on the authorize URL — required for SMART on FHIR (FHIR base URL).",
    )


class OAuthConfig(BaseModel):
    target_namespace: str | None = Field(
        default=None, description="K8s namespace to write token secrets to (auto-detected from pod if omitted)"
    )
    managed_by: str = Field(
        default="airlock", description="Value for app.kubernetes.io/managed-by label on managed secrets"
    )
    providers: list[OAuth2ProviderConfig] = Field(description="Provider configurations")
