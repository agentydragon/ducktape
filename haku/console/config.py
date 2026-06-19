"""Runtime settings for the Haku console (env-driven, prefix ``HAKU_CONSOLE_``)."""

from __future__ import annotations

from pathlib import Path

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="HAKU_CONSOLE_")

    # haku-state git access. The repo_url is the cluster-internal plaintext-HTTP
    # Forgejo (no TLS, so no CA bundle needed); credentials come from the
    # haku-state-git-write secret.
    git_repo_url: str
    git_username: str
    git_password: SecretStr
    branch: str = "main"

    clone_dir: Path = Path("/data/haku-state")
    pull_interval_s: float = 45.0

    host: str = "0.0.0.0"
    port: int = 8080
