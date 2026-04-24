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
