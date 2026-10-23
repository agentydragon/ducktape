"""Settings for the Study Casino backend."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Self

from pydantic import BeforeValidator, Field, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


@dataclass(frozen=True)
class OidcConfig:
    issuer: str
    client_id: str
    client_secret: str
    session_secret: bytes
    public_url: str


def _parse_csv_users(value: object) -> frozenset[str]:
    if value is None or value == "":
        return frozenset()
    if isinstance(value, frozenset):
        return value
    if isinstance(value, (set, list, tuple)):
        return frozenset(str(v).strip() for v in value if str(v).strip())
    if isinstance(value, str):
        return frozenset(s.strip() for s in value.split(",") if s.strip())
    raise TypeError(f"cannot parse admin_users from {value!r}")


# `NoDecode` disables pydantic-settings' default behaviour of running `json.loads`
# on env-var values for "complex" types (set/frozenset/list/dict/etc.) before any
# validators run. Without it, `STUDY_CASINO_ADMIN_USERS=agentydragon` would try
# to JSON-decode `agentydragon` (not a valid JSON literal) and crash at startup
# with `SettingsError: error parsing value for field "admin_users"` long before
# `_parse_csv_users` gets a chance to see the string. With NoDecode the raw env
# string flows straight to the BeforeValidator.
AdminUsers = Annotated[frozenset[str], NoDecode, BeforeValidator(_parse_csv_users)]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="STUDY_CASINO_", hide_input_in_errors=True)

    database_url: str = Field(
        repr=False, description="SQLAlchemy URI; PostgreSQL uses the standard `postgresql://` scheme."
    )
    host: str = "0.0.0.0"
    port: int = 8080
    frontend_dist_dir: Path | None = Field(
        default=None,
        description=(
            "Directory containing the built frontend bundle (index.html, main.js, "
            "sw.js, manifest.webmanifest, icon.svg). Defaults to `./frontend/dist` "
            "next to this module; override for tests or alternate layouts."
        ),
    )

    admin_users: AdminUsers = Field(
        default_factory=frozenset,
        description=(
            "Comma-separated list of usernames with admin privileges. Admins can "
            "manage prize catalogs for other users via `/admin/*`."
        ),
    )
    rng_secret: str | None = Field(
        default=None,
        min_length=32,
        repr=False,
        description=(
            "Secret key for deterministic casino RNG. Required when OIDC auth is enabled; "
            "dev/test mode falls back to an insecure fixed key when unset."
        ),
    )
    rng_key_id: str = Field(
        default="study-casino-rng-v1",
        description="Versioned identifier for the deterministic RNG secret used in audit rows.",
    )

    # OIDC — all four must be set together to enable authentication.
    # Empty or whitespace-only values are invalid. When all are unset, the
    # app accepts all requests as user "default" (dev/test mode).
    oidc_issuer: str | None = Field(
        default=None, description="OIDC issuer URL, e.g. https://auth.allegedly.works/application/o/study-casino"
    )
    oidc_client_id: str | None = Field(default=None, description="OAuth2 client_id registered in Authentik")
    oidc_client_secret: str | None = Field(
        default=None, repr=False, description="OAuth2 client_secret (confidential client)"
    )
    session_secret: str | None = Field(
        default=None,
        min_length=32,
        repr=False,
        description="Secret key for HMAC-signed session cookies. Must be ≥32 chars.",
    )
    public_url: str = Field(
        default="https://casino.allegedly.works",
        description="Public base URL of this app, used to build the OIDC redirect_uri.",
    )

    @model_validator(mode="after")
    def validate_oidc_configuration(self) -> Self:
        self.oidc_config()
        return self

    def oidc_config(self) -> OidcConfig | None:
        """Return OIDC config, or None only when all four settings are unset."""
        values = (
            ("oidc_issuer", self.oidc_issuer),
            ("oidc_client_id", self.oidc_client_id),
            ("oidc_client_secret", self.oidc_client_secret),
            ("session_secret", self.session_secret),
        )
        if all(value is None for _name, value in values):
            return None

        missing = [name for name, value in values if value is None or (isinstance(value, str) and not value.strip())]
        if missing:
            raise ValueError(f"OIDC settings must be configured together; missing or empty: {', '.join(missing)}")

        if (
            self.oidc_issuer is None
            or self.oidc_client_id is None
            or self.oidc_client_secret is None
            or self.session_secret is None
        ):
            raise ValueError("OIDC settings must be configured together")

        return OidcConfig(
            issuer=self.oidc_issuer,
            client_id=self.oidc_client_id,
            client_secret=self.oidc_client_secret,
            session_secret=self.session_secret.encode(),
            public_url=self.public_url,
        )

    def rng_secret_bytes(self) -> bytes:
        if self.rng_secret is not None:
            return self.rng_secret.encode()
        if self.oidc_config() is not None:
            raise ValueError("STUDY_CASINO_RNG_SECRET is required when OIDC authentication is enabled")
        return b"insecure-dev-only-study-casino-deterministic-rng-secret"

    def effective_rng_key_id(self) -> str:
        return self.rng_key_id if self.rng_secret is not None else "dev-insecure-fallback"
