"""Configuration for Authentik-backed MCP authentication."""

from urllib.parse import urlparse, urlunparse

from pydantic import BaseModel, ConfigDict, Field


def authentik_token_endpoint_for_issuer(issuer: str) -> str:
    """Global Authentik `/application/o/token/` URL derived from a per-provider issuer.

    Strips the trailing provider slug, preserving any reverse-proxy path prefix before
    `/application/o/`. Shared so every Authentik client (JWT-bearer exchange, refresh) derives the
    one global token endpoint the same way.
    """
    parsed = urlparse(issuer.rstrip("/"))
    prefix, marker, provider_slug = parsed.path.rpartition("/application/o/")
    if not marker or not provider_slug or "/" in provider_slug:
        raise ValueError(
            f"issuer must end in an Authentik per-provider issuer path like `.../application/o/<slug>/`; got {issuer!r}"
        )
    return urlunparse(parsed._replace(path=f"{prefix}{marker}token/"))


class DirectJwtTrust(BaseModel):
    """One explicitly trusted direct bearer-token issuer contract."""

    model_config = ConfigDict(frozen=True)

    issuer: str = Field(description="Exact JWT issuer, accepted with or without its trailing slash.")
    audiences: tuple[str, ...] = Field(min_length=1, description="Allowed JWT audience claims for this issuer.")
    required_scopes: tuple[str, ...] = Field(
        default=(), description="OAuth scopes every direct token from this issuer must contain."
    )


class AuthentikAuthConfig(BaseModel):
    """Auth-only config for an Authentik-backed MCP server.

    Core fields (oidc_issuer through public_base_url) are needed by
    `build_authentik_auth`. Exchange fields (proxy_client_id, exchange_timeout)
    are only needed when using `AuthentikTokenExchanger` for JWT-bearer token
    exchange against a proxy provider outpost.
    """

    model_config = ConfigDict(frozen=True)

    oidc_issuer: str
    oidc_client_id: str
    oidc_client_secret: str
    public_base_url: str
    proxy_client_id: str | None = None
    exchange_timeout: float = 10.0
    direct_jwt_trusts: tuple[DirectJwtTrust, ...] = Field(
        default=(),
        description="Direct machine-token issuers accepted alongside OIDCProxy. Each must share "
        "oidc_issuer's signing key because its JWKS validates the token. Audience and scopes are "
        "checked within each entry rather than combined across issuers.",
    )

    def normalized_public_base_url(self) -> str:
        return self.public_base_url.rstrip("/")

    def normalized_issuer(self) -> str:
        return self.oidc_issuer.rstrip("/")

    def authentik_token_endpoint(self) -> str:
        """Global Authentik `/application/o/token/` URL derived from `oidc_issuer`."""
        return authentik_token_endpoint_for_issuer(self.oidc_issuer)
