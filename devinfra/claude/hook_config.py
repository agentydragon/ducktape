"""Shared configuration loaded from .claude_hooks/config.yaml.

Repo-level config file that all hooks read. Configures k8s secrets,
OTEL tracing, and other shared settings. Environment variables
(DUCKTAPE_CLAUDE_HOOKS_*) override values from this file.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import BaseModel, Field

HOOKS_DOTDIR = ".claude_hooks"


class OtelConfig(BaseModel):
    endpoint: str | None = Field(default=None, description="OTLP/HTTP traces endpoint URL")
    bearer_token: str | None = Field(default=None, description="Bearer token for the OTLP endpoint")

    def with_env_overrides(self) -> OtelConfig:
        """Apply DUCKTAPE_CLAUDE_HOOKS_OTEL_* env var overrides.

        TODO: Rationalize env var override pattern — consider using pydantic-settings
        with a unified DUCKTAPE_CLAUDE_HOOKS_ prefix instead of ad-hoc os.environ.get().
        """
        return OtelConfig(
            endpoint=os.environ.get("DUCKTAPE_CLAUDE_HOOKS_OTEL_ENDPOINT", self.endpoint),
            bearer_token=os.environ.get("DUCKTAPE_CLAUDE_HOOKS_OTEL_AUTH_TOKEN", self.bearer_token),
        )


class SopsSecretSource(BaseModel):
    """Fetch a secret by decrypting a SOPS-encrypted YAML file."""

    kind: Literal["sops"]
    sops_file: str = Field(description="Repo-relative path to SOPS-encrypted YAML")
    key: str = Field(description="Key within the decrypted YAML")


class K8sSecretSource(BaseModel):
    """Fetch a secret from a Kubernetes Secret object."""

    kind: Literal["k8s"]
    secret_name: str
    key: str


SecretSource = Annotated[SopsSecretSource | K8sSecretSource, Field(discriminator="kind")]


class SecretsConfig(BaseModel):
    """Named secrets with tagged-union sources describing how to fetch each one."""

    k8s_token: SecretSource | None = None
    buildbuddy_api_key: SecretSource | None = None
    github_token: SecretSource | None = None
    otel_bearer_token: SecretSource | None = None


class K8sConfig(BaseModel):
    """K8s cluster connection config."""

    server: str = Field(description="K8s API server URL")
    service_account: str = Field(description="ServiceAccount name for kubeconfig user and context")
    service_account_namespace: str = Field(default="default", description="Namespace of the ServiceAccount")
    namespace: str = Field(description="Default namespace for kubectl operations")


class PreCommitConfig(BaseModel, frozen=True):
    """Pre-commit hook behavior configuration."""

    auto_apply_hooks: frozenset[str] = Field(
        default_factory=frozenset,
        description="Hook IDs whose file modifications are kept (not reverted). "
        "All other hooks' modifications are reverted and reported as diffs.",
    )
    show_report_diffs: bool = Field(
        default=False, description="Show unified diffs from report-only hooks in the PostToolUse output."
    )
    show_hook_output: bool = Field(
        default=False, description="Show stdout/stderr from failing hooks in the PostToolUse output."
    )


class HookConfig(BaseModel):
    """Top-level hook config file (.claude_hooks/config.yaml)."""

    k8s: K8sConfig | None = None
    secrets: SecretsConfig | None = None
    otel: OtelConfig | None = None
    pre_commit: PreCommitConfig | None = None
    extra_env_script: str | None = Field(
        default=None, description="Extra shell script content appended verbatim to the session env file."
    )

    @classmethod
    def load(cls, config_path: Path) -> HookConfig:
        """Load hook config from YAML file."""
        raw = yaml.safe_load(config_path.read_text())
        return cls.model_validate(raw)

    @classmethod
    def load_from_repo(cls, root: Path) -> HookConfig | None:
        """Load hook config from repo root (with env var overrides), or None if not found."""
        config_path = root / HOOKS_DOTDIR / "config.yaml"
        if not config_path.exists():
            return None
        config = cls.load(config_path)
        if config.otel:
            config = config.model_copy(update={"otel": config.otel.with_env_overrides()})
        return config
