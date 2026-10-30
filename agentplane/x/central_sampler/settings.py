"""Typed JSON configuration schema for the central sampler Go service."""

from ipaddress import ip_address
from pathlib import Path
from typing import ClassVar, Literal, Self
from urllib.parse import urlsplit

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
    direct_executor_target: str | None = None
    direct_executor_insecure_localtest: bool = False
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

    @model_validator(mode="after")
    def exactly_one_execution_target(self) -> Self:
        gateway = self.gateway_target is not None
        direct = self.direct_executor_target is not None
        if gateway == direct:
            raise ValueError("configure exactly one gateway or direct executor target")
        if direct != self.direct_executor_insecure_localtest:
            raise ValueError("direct executor target and explicit insecure localtest mode must be configured together")
        if self.direct_executor_target is not None:
            parsed = urlsplit("//" + self.direct_executor_target)
            try:
                port = parsed.port
                address = ip_address(parsed.hostname or "")
            except ValueError as error:
                raise ValueError("insecure direct executor target must use a loopback IP address and port") from error
            if (
                parsed.hostname is None
                or port is None
                or not address.is_loopback
                or parsed.path
                or parsed.query
                or parsed.fragment
                or parsed.username is not None
                or parsed.password is not None
            ):
                raise ValueError("insecure direct executor target must use a loopback IP address and port")
        return self

    @field_serializer("allowed_accounts")
    def serialize_allowed_accounts(self, value: set[str]) -> list[str]:
        return sorted(value)
