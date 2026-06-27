"""Runtime settings for the Haku UI backend (env-driven, prefix ``HAKU_UI_``).

Git credentials come from the ``haku-state-git-write`` secret, mounted by the
Deployment. The internal Forgejo URL (``http://forgejo-http.forgejo:3000/...``) is
the cluster-internal plaintext-HTTP host (no TLS), reachable from haku-sandbox.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="HAKU_UI_")

    # haku-state git access (the cluster-internal plaintext-HTTP Forgejo). Credentials
    # come from the haku-state-git-write secret.
    git_repo_url: str = "http://forgejo-http.forgejo:3000/haku/haku-state.git"
    git_username: str
    git_password: SecretStr
    branch: str = "main"

    clone_dir: Path = Path("/data/haku-state")
    pull_interval_s: float = 45.0

    # Directory holding the built React SPA (index.html + assets), served same-origin.
    # The Dockerfile sets this to the bundled dir; unset → API-only (e.g. local dev).
    static_dir: Path | None = None
