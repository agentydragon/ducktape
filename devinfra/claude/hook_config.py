"""Shared configuration loaded from .claude_hooks/config.yaml.

Repo-level config file that all hooks read. Configures k8s secrets,
OTEL tracing, and other shared settings. Environment variables
(DUCKTAPE_CLAUDE_HOOKS_*) override values from this file.
"""

from __future__ import annotations

import os
from pathlib import Path

import yaml
from pydantic import BaseModel, Field, model_validator

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


class K8sSecretMapping(BaseModel):
    """Maps a single k8s Secret's data keys to env var names."""

    name: str
    data: dict[str, str] = Field(description="Secret data key → env var name")


class K8sSecretRef(BaseModel):
    """Reference to a single key in a k8s Secret."""

    secret_name: str
    data_key: str


class K8sSecretsConfig(BaseModel):
    """Config for reading secrets from k8s."""

    namespace: str
    secrets: list[K8sSecretMapping]
    buildbuddy_api_key: K8sSecretRef | None = None
    otel_bearer_token: K8sSecretRef | None = None

    @model_validator(mode="after")
    def _validate_no_duplicate_env_vars(self) -> K8sSecretsConfig:
        seen: dict[str, str] = {}
        for entry in self.secrets:
            for env_var in entry.data.values():
                if env_var in seen:
                    raise ValueError(f"Duplicate env var {env_var!r} in secrets {seen[env_var]!r} and {entry.name!r}")
                seen[env_var] = entry.name
        return self


class K8sConfig(BaseModel):
    """K8s cluster connection config."""

    server: str = Field(description="K8s API server URL")
    service_account: str = Field(description="ServiceAccount name for kubeconfig user and context")
    service_account_namespace: str = Field(default="default", description="Namespace of the ServiceAccount")
    namespace: str = Field(description="Default namespace for kubectl operations")


class PreCommitConfig(BaseModel):
    """Pre-commit hook behavior configuration."""

    auto_apply_hooks: set[str] = Field(
        description="Hook IDs whose file modifications are kept (not reverted). "
        "All other hooks' modifications are reverted and reported as diffs."
    )


class HookConfig(BaseModel):
    """Top-level hook config file (.claude_hooks/config.yaml)."""

    k8s: K8sConfig | None = None
    k8s_secrets: K8sSecretsConfig | None = None
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
