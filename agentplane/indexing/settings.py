"""Deployment settings for the Git index service."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from haku.recall_index.chunking import DEFAULT_CHUNK_BUDGET, ChunkBudget


class SnapshotLimits(BaseModel):
    model_config = ConfigDict(frozen=True)

    total_bytes: int = Field(default=512 * 1024 * 1024, gt=0)
    file_bytes: int = Field(default=8 * 1024 * 1024, gt=0)
    entries: int = Field(default=100_000, gt=0)


def _lines(value: object) -> object:
    if isinstance(value, str):
        return tuple(line.strip() for line in value.splitlines() if line.strip())
    return value


# NoDecode keeps pydantic-settings from JSON-decoding the environment value, so a ConfigMap
# can hold the patterns one per line exactly as a .gitignore would.
IgnorePatterns = Annotated[tuple[str, ...], NoDecode, BeforeValidator(_lines)]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AGENTPLANE_INDEX_", cli_parse_args=True, cli_kebab_case=True)

    database_url: SecretStr = Field(description="Dedicated PostgreSQL database, postgresql+asyncpg:// URL.")
    repository_url: str = Field(description="Git remote to index: an HTTP(S) URL or a local path.")
    branch: str
    checkout_dir: Path = Field(description="Directory for the bare clone; created on first use.")
    git_username: str | None = None
    git_password: SecretStr | None = None
    git_ca_bundle: Path | None = Field(
        default=None, description="PEM bundle for HTTPS remotes; libgit2 does not read SSL_CERT_FILE."
    )
    ignore: IgnorePatterns = Field(
        default=(), description="gitignore-syntax patterns, one per line; matching paths are left out of snapshots."
    )
    embedding_url: str
    embedding_model: str
    embedding_api_key: SecretStr
    read_token: SecretStr = Field(min_length=1, description="Bearer credential required for search and status.")
    query_instruction: str = ""
    embedding_timeout_seconds: float = Field(default=60, gt=0)
    poll_seconds: float = Field(default=30, gt=0)
    gc_seconds: float = Field(default=3600, gt=0)
    gc_grace_seconds: float = Field(default=86400, ge=0)
    chunk_budget: ChunkBudget = DEFAULT_CHUNK_BUDGET
    snapshot_limits: SnapshotLimits = SnapshotLimits()
    host: str = "0.0.0.0"
    port: int = Field(default=8080, ge=1, le=65535)

    def __init__(self, **values: Any) -> None:
        super().__init__(**values)

    @model_validator(mode="after")
    def _paired_credentials(self) -> Settings:
        if (self.git_username is None) != (self.git_password is None):
            raise ValueError("git_username and git_password must be set together")
        return self
