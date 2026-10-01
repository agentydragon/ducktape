"""Shared schema for the generated Attic JWT roster and its runtime rotator."""

from pathlib import Path

from pydantic import BaseModel, Field


class Token(BaseModel):
    name: str = Field(description="Human-readable name for logs and the commit message")
    sops_file: Path = Field(description="Repo-relative path to the encrypted output file")
    sub: str = Field(description="JWT subject claim passed to atticadm --sub")
    validity: str = Field(description="Token lifetime passed to atticadm --validity (e.g. '1 year')")
    pull: list[str] = Field(description="Caches the token may pull from (atticadm --pull, repeated)")
    push: list[str] = Field(
        default_factory=list,
        description="Caches the token may push to (atticadm --push, repeated); empty for read-only tokens",
    )


class Config(BaseModel):
    tokens: list[Token]
    attic_namespace: str = Field(
        default="nix-cache", description="Namespace of the attic deployment exec'd for minting"
    )
    attic_deployment: str = Field(default="deploy/attic", description="Deployment exec'd for minting")
    server_config: str = Field(
        default="/config/server.toml",
        description="Attic server config path (inside the attic pod) passed to atticadm -f",
    )
    token_field: str = Field(default="attic_token", description="YAML field name under which the JWT is written")
    rotate_below_hours: int = Field(
        default=24, description="Mint a fresh token once remaining validity drops below this"
    )
    git_remote: str = Field(
        default="https://github.com/agentydragon/ducktape.git",
        description="Full git remote URL to clone + push (host-agnostic; any HTTPS git server works)",
    )
    git_username: str = Field(
        default="x-access-token",
        description="Username paired with the token for HTTPS auth (GitHub PATs use 'x-access-token')",
    )
    git_clone_depth: int | None = Field(
        default=1,
        description="Shallow-clone depth for git_remote; None for a full clone (the local transport can't shallow-fetch)",
    )
    sops_config: str = Field(default=".sops.yaml", description="Repo path sops reads to pick the recipient set")
    git_author_name: str = "attic-jwt-rotation"
    git_author_email: str = "noreply@allegedly.works"
