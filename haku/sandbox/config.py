"""Deploy-reviewed configuration for the Kubernetes Agent Sandbox environment."""

from __future__ import annotations

import hashlib

from pydantic import BaseModel, ConfigDict, Field, model_validator

_DNS_LABEL = r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$"


class SandboxConfig(BaseModel):
    """The one Agent Sandbox environment Console hands out."""

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

    @property
    def script_digest(self) -> str:
        """Identity of the script text, stamped on a claim when its bootstrap runs.

        Truncated because it is only ever compared for equality and is quoted verbatim into
        agent-facing warnings, where a full SHA-256 is noise.
        """

        return hashlib.sha256(self.script.encode()).hexdigest()[:16]


class SandboxEnvironmentConfig(BaseModel):
    """Non-secret deploy configuration for that environment and its reviewed bootstrap."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    sandbox: SandboxConfig
    bootstrap: BootstrapConfig

    @model_validator(mode="after")
    def _validate_lifecycle_budgets(self) -> SandboxEnvironmentConfig:
        minimum_initial_ttl = self.sandbox.provisioning_timeout_seconds + self.bootstrap.timeout_seconds
        if self.sandbox.initial_ttl_seconds <= minimum_initial_ttl:
            raise ValueError(
                "sandbox.initial_ttl_seconds must exceed provisioning_timeout_seconds plus bootstrap.timeout_seconds"
            )
        if self.sandbox.exec_ttl_extension_seconds < self.sandbox.max_exec_timeout_seconds:
            raise ValueError("sandbox.exec_ttl_extension_seconds must be at least sandbox.max_exec_timeout_seconds")
        return self
