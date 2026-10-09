"""The rotator's configuration schema: the roster `rotate.py --config` reads.

Kept apart from `rotate.py` so the cdk8s generator (cluster/cdk8s/forgejo/token_rotation.py),
which builds the roster from these models, imports pydantic and nothing of the runtime.
"""

from pathlib import Path

from pydantic import BaseModel, Field

FULL_ACCOUNT_SCOPES = [
    "write:activitypub",
    "write:issue",
    "write:misc",
    "write:notification",
    "write:organization",
    "write:package",
    "write:repository",
    "write:user",
]


class TeaSecretOutput(BaseModel):
    """Optional k8s Secret manifest carrying a mounted `tea` config."""

    path: Path = Field(description="Repo-relative path for the Secret manifest (under cluster/k8s/, *.sops.yaml)")
    name: str
    namespace: str
    annotations: dict[str, str] = Field(
        default_factory=dict, description="Additional plaintext metadata annotations to preserve on each rotation"
    )
    config_key: str = "config.yml"
    token_key: str = "token"
    username_key: str = "username"
    url_key: str = "url"
    token_name_key: str = "token-name"
    token_last_eight_key: str = "token-last-eight"


class Rotation(BaseModel):
    name: str = Field(description="Human-readable name for logs and commit messages")
    credentials_dir: Path = Field(description="Mounted secret dir with username/password and optional url/internal_url")
    sops_file: Path = Field(description="Repo-relative path to the encrypted raw-token output")
    token_field: str = Field(default="token", description="YAML field name for the raw token in sops_file")
    token_prefix: str | None = Field(
        default=None,
        description="Prefix for Forgejo token names. Defaults to rotation.name; minted names append UTC timestamp.",
    )
    scopes: list[str] = Field(
        default_factory=lambda: list(FULL_ACCOUNT_SCOPES),
        description="Forgejo token scopes. Defaults to every non-admin write scope.",
    )
    rotate_after_days: int = Field(default=30, description="Mint a fresh token once the current one is this old")
    keep_previous: int = Field(
        default=1,
        description="After committing a new token, keep this many older rotator-created tokens as rollback cushion",
    )
    api_url: str = Field(
        default="http://forgejo-http.forgejo:3000",
        description="Forgejo base URL used by the rotator API client; credentials_dir/internal_url overrides it",
    )
    tea_url: str = Field(
        default="https://git.allegedly.works",
        description="Forgejo URL written into tea config; credentials_dir/url overrides it",
    )
    login_name: str = Field(default="forgejo", description="tea login name")
    tea_secret: TeaSecretOutput | None = Field(
        default=None, description="When set, also write a k8s Secret manifest with config.yml + raw token"
    )

    @property
    def token_name_prefix(self) -> str:
        return self.token_prefix or self.name


class Config(BaseModel):
    rotations: list[Rotation]
    github_repo: str = "agentydragon/ducktape"
    sops_config: str = Field(default=".sops.yaml", description="Repo path sops reads to pick the recipient set")
    git_author_name: str = "forgejo-token-rotation"
    git_author_email: str = "noreply@allegedly.works"
