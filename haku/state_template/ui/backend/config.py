"""Runtime settings for the Haku UI backend (env-driven, prefix ``HAKU_UI_``).

Credentials come from the ``haku-state-git-write`` secret, mounted by the Deployment,
and are used as Forgejo basic auth. The internal Forgejo URL
(``http://forgejo-http.forgejo:3000/...``) is the cluster-internal plaintext-HTTP host
(no TLS), reachable from haku-sandbox.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="HAKU_UI_")

    # haku-state via the cluster-internal plaintext-HTTP Forgejo API. The repo API root
    # (…/api/v1/repos/<owner>/<repo>); credentials (basic auth) come from the
    # haku-state-git-write secret.
    forgejo_api_url: str = "http://forgejo-http.forgejo:3000/api/v1/repos/haku/haku-state"
    git_username: str
    git_password: SecretStr
    branch: str = "main"

    # Directory holding the built React SPA (index.html + assets), served same-origin.
    # The Dockerfile sets this to the bundled dir; unset → API-only (e.g. local dev).
    static_dir: Path | None = None

    # The git commit the running image was built from (baked by CI via --build-arg GIT_SHA →
    # HAKU_UI_GIT_SHA); None in local dev. Surfaced in the UI footer as a Forgejo commit link.
    git_sha: str | None = None
    # Public (operator-facing) Forgejo repo URL, for building commit links. NOT the internal
    # API URL (forgejo_api_url) — that's cluster-internal plaintext HTTP.
    repo_web_url: str = "https://git.allegedly.works/haku/haku-state"
