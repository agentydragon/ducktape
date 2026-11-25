"""Deployment settings for the SSH MCP server."""

from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator

_MAX_OUTPUT_BYTES = 100_000


class TargetConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    host: str = Field(min_length=1, max_length=253)
    user: str = Field(min_length=1, max_length=128)
    identity_file: Path
    port: int = Field(default=22, ge=1, le=65535)


class SshSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    known_hosts_file: Path
    targets: list[TargetConfig] = Field(min_length=1)
    command_timeout_seconds: int = Field(default=300, ge=1, le=86_400)
    connect_timeout_seconds: int = Field(default=10, ge=1, le=300)
    output_limit_bytes: int = Field(default=64 * 1024, ge=1, le=_MAX_OUTPUT_BYTES)
    max_concurrent_executions: int = Field(default=4, ge=1, le=64)

    @field_validator("targets")
    @classmethod
    def unique_targets(cls, targets: list[TargetConfig]) -> list[TargetConfig]:
        tuples = [(target.host, target.user) for target in targets]
        if len(set(tuples)) != len(tuples):
            raise ValueError("SSH target (host, user) tuples must be unique")
        return targets
