"""Typed JSON configuration schema for the central sampler Go service."""

from pathlib import Path
from typing import ClassVar, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_serializer, model_validator
from pydantic_settings import SettingsConfigDict


class ResponsesConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dialect: Literal["lite", "standard"]


class ModelConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    api: Literal["responses"]
    responses: ResponsesConfig
    exec_encoding: Literal["freeform", "json"]
    reasoning_efforts: set[str] = Field(min_length=1)

    @field_serializer("reasoning_efforts")
    def serialize_reasoning_efforts(self, value: set[str]) -> list[str]:
        return sorted(value)


class Settings(BaseModel):
    config_file_env: ClassVar[str] = "AGENTPLANE_CENTRAL_SAMPLER_CONFIG"
    model_config = SettingsConfigDict(extra="forbid", env_prefix="AGENTPLANE_CENTRAL_SAMPLER_")

    database_url: str | None = None
    listen: str
    service_name: str
    llm_proxy_url: str
    credential: SecretStr | None = None
    credential_file: Path | None = None
    models: dict[str, ModelConfig] = Field(min_length=1)

    audience: str
    allowed_accounts: set[str] = Field(min_length=1)
    code_mode_host: Path
    executor_codex: str
    logs: Path
    history_target: str | None = None
    history_token_file: Path | None = None
    gateway_target: str | None = None
    gateway_token_file: Path | None = None
    direct_executor_url: str | None = None
    max_live_sessions: int = Field(gt=0)
    turn_timeout_seconds: int = Field(gt=0)
    rpc_timeout_seconds: int = Field(gt=0)
    lease_seconds: int = Field(gt=30)
    auto_compact_tokens: int | None = Field(default=None, gt=0)
    kubeconfig: Path | None = None

    @model_validator(mode="after")
    def exactly_one_credential_source(self) -> Self:
        if (self.credential is None) == (self.credential_file is None):
            raise ValueError("configure exactly one of credential and credential_file")
        return self

    @field_serializer("allowed_accounts")
    def serialize_allowed_accounts(self, value: set[str]) -> list[str]:
        return sorted(value)
