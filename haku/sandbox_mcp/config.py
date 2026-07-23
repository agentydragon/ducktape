"""Configuration for the single-environment Haku sandbox MCP server."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_DNS_LABEL = r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$"


class SandboxConfig(BaseModel):
    """The one Agent Sandbox environment managed by this server."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    namespace: str = Field(min_length=1, max_length=63, pattern=_DNS_LABEL)
    warm_pool: str = Field(min_length=1, max_length=63, pattern=_DNS_LABEL)
    container: str = Field(min_length=1, max_length=63, pattern=_DNS_LABEL)
    default_cwd: str = Field(min_length=1)
    initial_ttl_seconds: int = Field(gt=0)
    exec_ttl_extension_seconds: int = Field(gt=0)
    provisioning_timeout_seconds: int = Field(gt=0)
    max_exec_timeout_seconds: int = Field(gt=0, le=3600)
    max_output_bytes: int = Field(gt=0, le=1_000_000)


class BootstrapConfig(BaseModel):
    """The reviewed bootstrap run once after a sandbox becomes ready."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    cwd: str = Field(min_length=1)
    timeout_seconds: int = Field(gt=0, le=3600)
    script: str = Field(min_length=1)


class EnvironmentConfig(BaseModel):
    """Non-secret YAML configuration for the server's one environment."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    sandbox: SandboxConfig
    bootstrap: BootstrapConfig

    @model_validator(mode="after")
    def _validate_lifecycle_budgets(self) -> EnvironmentConfig:
        minimum_initial_ttl = self.sandbox.provisioning_timeout_seconds + self.bootstrap.timeout_seconds
        if self.sandbox.initial_ttl_seconds <= minimum_initial_ttl:
            raise ValueError(
                "sandbox.initial_ttl_seconds must exceed provisioning_timeout_seconds plus bootstrap.timeout_seconds"
            )
        if self.sandbox.exec_ttl_extension_seconds < self.sandbox.max_exec_timeout_seconds:
            raise ValueError("sandbox.exec_ttl_extension_seconds must be at least sandbox.max_exec_timeout_seconds")
        return self

    @property
    def contract_hash(self) -> str:
        """Stable identity for claims created under this exact environment contract."""

        payload = json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode()).hexdigest()


class ServerSettings(BaseSettings):
    """Secret/process settings sourced only from ``HAKU_SANDBOX_MCP_*`` env vars."""

    model_config = SettingsConfigDict(env_prefix="HAKU_SANDBOX_MCP_", extra="forbid")

    config_file: Path
    bearer_token: SecretStr
    host: str = "0.0.0.0"
    port: int = Field(default=8765, ge=1, le=65535)

    def load_environment(self) -> EnvironmentConfig:
        try:
            raw = yaml.safe_load(self.config_file.read_text())
        except OSError as error:
            raise ValueError(f"could not read sandbox config {self.config_file}: {error}") from error
        return EnvironmentConfig.model_validate(raw)
