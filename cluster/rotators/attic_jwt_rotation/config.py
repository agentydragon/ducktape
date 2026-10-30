"""Shared schema for the generated Attic JWT roster and its runtime rotator."""

from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class KubernetesSecretDestination(BaseModel):
    namespace: str = Field(min_length=1)
    name: str = Field(min_length=1)
    key: str = Field(min_length=1)


class WorkspaceNotification(BaseModel):
    workspace: str = Field(min_length=1)
    annotation_key: str = Field(min_length=1)


class KubernetesPublication(BaseModel):
    model_config = ConfigDict(extra="forbid")

    secret: KubernetesSecretDestination
    notification: WorkspaceNotification


class SopsDestination(BaseModel):
    model_config = ConfigDict(extra="forbid")

    storage: Literal["sops"] = "sops"
    sops_file: Path = Field(description="Repo-relative path to the encrypted output file")


class KubernetesDestination(KubernetesPublication):
    storage: Literal["kubernetes"] = "kubernetes"


class MirroredDestination(KubernetesPublication):
    storage: Literal["mirrored"] = "mirrored"
    sops_file: Path = Field(description="Repo-relative path to the encrypted output file")


Destination = Annotated[SopsDestination | KubernetesDestination | MirroredDestination, Field(discriminator="storage")]


class Token(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(description="Human-readable name for logs and the commit message")
    sub: str = Field(description="JWT subject claim passed to atticadm --sub")
    validity: str = Field(description="Token lifetime passed to atticadm --validity (e.g. '1 year')")
    pull: list[str] = Field(description="Caches the token may pull from (atticadm --pull, repeated)")
    push: list[str] = Field(
        default_factory=list,
        description="Caches the token may push to (atticadm --push, repeated); empty for read-only tokens",
    )
    destination: Destination


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

    @model_validator(mode="before")
    @classmethod
    def adapt_legacy_tokens(cls, value: Any) -> Any:
        """Adapt deployed flat token records while ConfigMaps roll to nested destinations.

        CLEANUP(added 2026-10-30): remove the flat-record branch after every deployed
        rotator ConfigMap contains nested destinations.
        """
        if not isinstance(value, dict):
            return value
        raw_tokens = value.get("tokens")
        if not isinstance(raw_tokens, list):
            return value

        tokens = []
        for raw_token in raw_tokens:
            if not isinstance(raw_token, dict) or "destination" in raw_token:
                tokens.append(raw_token)
                continue

            # The deployed schema required sops_file; its optional kubernetes
            # field distinguished mirrored tokens. Kubernetes-only records were
            # never valid in that schema and must wait for the new image.
            if "sops_file" not in raw_token:
                tokens.append(raw_token)
                continue

            legacy_kubernetes = raw_token.get("kubernetes")
            legacy_storage = raw_token.get("storage")
            inferred_storage = "mirrored" if "kubernetes" in raw_token else "sops"
            if "storage" in raw_token and legacy_storage != inferred_storage:
                raise ValueError("flat Attic token storage tag contradicts its destination fields")
            destination: dict[str, Any]
            if "kubernetes" in raw_token:
                if not isinstance(legacy_kubernetes, dict):
                    # Preserve the malformed value so nested destination validation rejects it.
                    destination = {
                        "storage": "mirrored",
                        "sops_file": raw_token["sops_file"],
                        "kubernetes": legacy_kubernetes,
                    }
                else:
                    destination = {"storage": "mirrored", "sops_file": raw_token["sops_file"], **legacy_kubernetes}
            else:
                destination = {"storage": "sops", "sops_file": raw_token["sops_file"]}

            tokens.append(
                {
                    **{
                        key: item
                        for key, item in raw_token.items()
                        if key not in {"sops_file", "kubernetes", "storage"}
                    },
                    "destination": destination,
                }
            )
        return {**value, "tokens": tokens}
