"""Settings for the Study Casino backend."""

from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="STUDY_CASINO_")

    data_dir: Path = Field(
        default=Path("/data"),
        description="Directory containing the SQLite state database. "
        "Should be backed by a PersistentVolume in production.",
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

    # OIDC — all four must be set together to enable authentication.
    # When unset, the app accepts all requests as user "default" (dev/test mode).
    oidc_issuer: str | None = Field(
        default=None, description="OIDC issuer URL, e.g. https://auth.allegedly.works/application/o/study-casino"
    )
    oidc_client_id: str | None = Field(default=None, description="OAuth2 client_id registered in Authentik")
    oidc_client_secret: str | None = Field(default=None, description="OAuth2 client_secret (confidential client)")
    session_secret: str | None = Field(
        default=None, description="Secret key for HMAC-signed session cookies. Any string ≥32 chars."
    )
    public_url: str = Field(
        default="https://casino.allegedly.works",
        description="Public base URL of this app, used to build the OIDC redirect_uri.",
    )

    @property
    def oidc_enabled(self) -> bool:
        return all([self.oidc_issuer, self.oidc_client_id, self.oidc_client_secret, self.session_secret])
