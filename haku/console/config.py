"""Runtime settings for the Haku console (env-driven, prefix ``HAKU_CONSOLE_``)."""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class LaunchRoutineConfig(BaseModel):
    """The `launch-routine` capability's target: the public Anthropic fire URL plus
    the bearer that authorizes it. Both come from the `haku-routine-launch-token`
    secret / deployment env; set together or not at all (the capability is disabled
    when unset). The token lives only in the haku-console namespace — Haku can't
    read it."""

    url: str
    token: SecretStr


class Settings(BaseSettings):
    # env_nested_delimiter so launch_routine.{url,token} read from
    # HAKU_CONSOLE_LAUNCH_ROUTINE__URL / __TOKEN.
    model_config = SettingsConfigDict(env_prefix="HAKU_CONSOLE_", env_nested_delimiter="__")

    # haku-state git access. The repo_url is the cluster-internal plaintext-HTTP
    # Forgejo (no TLS, so no CA bundle needed); credentials come from the
    # haku-state-git-write secret.
    git_repo_url: str
    git_username: str
    git_password: SecretStr
    branch: str = "main"

    clone_dir: Path = Path("/data/haku-state")
    pull_interval_s: float = 45.0

    # Directory holding the built React SPA (index.html + assets), served same-origin.
    # Unset in tests (the API runs without a UI); set to the bundled dir in the image.
    static_dir: Path | None = None

    # Capability tier. launch_routine enables POST /api/capabilities/launch-routine
    # (None → the capability returns 503). csrf_secret signs the double-submit CSRF
    # tokens that gate the capability tier; when unset, create_app generates an
    # ephemeral one at startup (fine for the single-replica console — a restart just
    # makes the SPA refetch its token).
    launch_routine: LaunchRoutineConfig | None = None
    csrf_secret: SecretStr | None = None
