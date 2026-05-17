"""Settings for the Study Casino backend."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

from pydantic import BeforeValidator, Field
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
    model_config = SettingsConfigDict(env_prefix="STUDY_CASINO_")

    database_url: str = Field(
        description="SQLAlchemy URL for the casino state database (e.g. `postgresql+psycopg://user:pass@host/db`)."
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

    # OIDC — all four must be set together to enable authentication.
    # When unset, the app accepts all requests as user "default" (dev/test mode).
    oidc_issuer: str | None = Field(
        default=None, description="OIDC issuer URL, e.g. https://auth.allegedly.works/application/o/study-casino"
    )
    oidc_client_id: str | None = Field(default=None, description="OAuth2 client_id registered in Authentik")
    oidc_client_secret: str | None = Field(default=None, description="OAuth2 client_secret (confidential client)")
    session_secret: str | None = Field(
        default=None, min_length=32, description="Secret key for HMAC-signed session cookies. Must be ≥32 chars."
    )
    public_url: str = Field(
        default="https://casino.allegedly.works",
        description="Public base URL of this app, used to build the OIDC redirect_uri.",
    )

    def oidc_config(self) -> OidcConfig | None:
        """Return fully-typed OIDC config if all four fields are set, else None."""
        if not (self.oidc_issuer and self.oidc_client_id and self.oidc_client_secret and self.session_secret):
            return None
        return OidcConfig(
            issuer=self.oidc_issuer,
            client_id=self.oidc_client_id,
            client_secret=self.oidc_client_secret,
            session_secret=self.session_secret.encode(),
            public_url=self.public_url,
        )
