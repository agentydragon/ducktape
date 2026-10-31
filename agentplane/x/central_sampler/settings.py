"""Typed JSON configuration schema for the central sampler Go service."""

from ipaddress import ip_address
from pathlib import Path
from typing import ClassVar, Literal, Self
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_serializer, field_validator, model_validator
from pydantic_settings import SettingsConfigDict

DEFAULT_EXECUTOR_INITIALIZE_TIMEOUT_SECONDS = 30
MAX_EXECUTOR_INITIALIZE_TIMEOUT_SECONDS = (2**63 - 1) // 1_000_000_000


class AuthConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["bearer", "anthropic_api_key"]
    credential: SecretStr | None = None
    credential_file: Path | None = None

    @field_validator("credential_file", mode="before")
    @classmethod
    def nonempty_credential_file(cls, value: object) -> object:
        if value == "":
            raise ValueError("credential_file must not be empty")
        return value

    @model_validator(mode="after")
    def exactly_one_credential_source(self) -> Self:
        if (self.credential is None) == (self.credential_file is None):
            raise ValueError("configure exactly one of credential and credential_file")
        if self.credential is not None and not self.credential.get_secret_value():
            raise ValueError("credential must not be empty")
        return self

    def read_credential(self) -> str:
        if self.credential is not None:
            return self.credential.get_secret_value()
        if self.credential_file is None:
            raise ValueError("configure exactly one of credential and credential_file")
        credential = self.credential_file.read_text().strip()
        if not credential:
            raise ValueError("provider credential file is empty")
        return credential


class ConnectionConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base_url: str
    auth: AuthConfig

    @field_validator("base_url")
    @classmethod
    def safe_base_url(cls, value: str) -> str:
        parsed = urlsplit(value)
        try:
            port = parsed.port
        except ValueError as error:
            raise ValueError("base_url must use a valid host and port") from error
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.netloc.endswith(":")
            or (port is not None and port == 0)
            or parsed.username is not None
            or parsed.password is not None
            or "?" in value
            or "#" in value
            or any(character.isspace() or ord(character) < 0x20 for character in value)
        ):
            raise ValueError("base_url must be a safe http(s) URL without credentials, query, or fragment")
        return value


class ResponsesConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dialect: Literal["lite", "standard"]


class AnthropicBlockBindingConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    prefix_mismatch_behavior: Literal["error", "drop_block"]


class AnthropicThinkingConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["adaptive", "disabled", "enabled"]
    budget_tokens: int | None = Field(default=None, gt=0)
    block_binding: AnthropicBlockBindingConfig | None = None

    @model_validator(mode="after")
    def check_budget(self) -> Self:
        if self.type == "enabled" and self.budget_tokens is None:
            raise ValueError("thinking.type enabled requires positive budget_tokens")
        if self.type != "enabled" and self.budget_tokens is not None:
            raise ValueError("thinking.budget_tokens is only valid with thinking.type enabled")
        return self


class AnthropicPromptCacheConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ttl: Literal["5m", "1h"]


class AnthropicCompactionConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_tokens: int = Field(gt=0)
    auto_compact_tokens: int | None = Field(default=None, gt=0)
    keep_recent_turns: int = Field(default=0, ge=0)


class AnthropicMessagesConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_tokens: int = Field(gt=0)
    thinking: AnthropicThinkingConfig | None = None
    prompt_cache: AnthropicPromptCacheConfig | None = None
    cache_diagnostics: bool = False
    per_message_effort: bool = False
    strict_tool_input: bool = False
    eager_input_streaming: bool = False
    compaction: AnthropicCompactionConfig | None = None

    @model_validator(mode="after")
    def check_per_message_effort(self) -> Self:
        if self.thinking is not None:
            if (
                self.thinking.type == "enabled"
                and self.thinking.budget_tokens is not None
                and self.thinking.budget_tokens >= self.max_tokens
            ):
                raise ValueError("thinking.budget_tokens must be less than max_tokens")
            if self.thinking.block_binding is not None and self.thinking.type != "adaptive":
                raise ValueError("thinking.block_binding requires thinking.type adaptive")
        if self.per_message_effort and (self.thinking is None or self.thinking.type != "adaptive"):
            raise ValueError("per_message_effort requires thinking.type adaptive")
        return self


class ModelConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    connection: str
    provider_model: str
    api: Literal["responses", "anthropic_messages"]
    responses: ResponsesConfig | None = None
    anthropic_messages: AnthropicMessagesConfig | None = None
    exec_encoding: Literal["freeform", "json"]
    reasoning_efforts: set[str] = Field(min_length=1)

    @field_serializer("reasoning_efforts")
    def serialize_reasoning_efforts(self, value: set[str]) -> list[str]:
        return sorted(value)

    @model_validator(mode="after")
    def check_api_settings(self) -> Self:
        if self.connection == "":
            raise ValueError("model connection must not be empty")
        if self.provider_model == "":
            raise ValueError("model provider_model must not be empty")
        if self.api == "responses":
            if self.responses is None:
                raise ValueError("responses settings are required for the responses API")
            if self.anthropic_messages is not None:
                raise ValueError("anthropic_messages settings are only valid for the anthropic_messages API")
        else:
            if self.responses is not None:
                raise ValueError("responses settings are only valid for the responses API")
            if self.exec_encoding != "json":
                raise ValueError("anthropic_messages requires exec_encoding json")
            if self.anthropic_messages is None:
                raise ValueError("anthropic_messages settings are required for the anthropic_messages API")
            unsupported_efforts = self.reasoning_efforts - {"low", "medium", "high", "xhigh", "max"}
            if unsupported_efforts:
                raise ValueError(f"unsupported Anthropic reasoning efforts: {sorted(unsupported_efforts)}")
        return self


class Settings(BaseModel):
    config_file_env: ClassVar[str] = "AGENTPLANE_CENTRAL_SAMPLER_CONFIG"
    model_config = SettingsConfigDict(extra="forbid", env_prefix="AGENTPLANE_CENTRAL_SAMPLER_")

    database_url: str | None = None
    listen: str
    connections: dict[str, ConnectionConfig] = Field(min_length=1)
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
    executor_initialize_timeout_seconds: int = Field(
        default=DEFAULT_EXECUTOR_INITIALIZE_TIMEOUT_SECONDS,
        gt=0,
        le=MAX_EXECUTOR_INITIALIZE_TIMEOUT_SECONDS,
        description="Time allowed for the executor OpenSession initialization handshake.",
    )
    lease_seconds: int = Field(gt=30)
    auto_compact_tokens: int | None = Field(default=None, gt=0)
    kubeconfig: Path | None = None

    @model_validator(mode="after")
    def validate_model_connections(self) -> Self:
        if any(not name for name in self.connections):
            raise ValueError("connection name must not be empty")
        for model, config in self.models.items():
            if not model:
                raise ValueError("model route must not be empty")
            connection = self.connections.get(config.connection)
            if connection is None:
                raise ValueError(f"model {model} references unknown connection {config.connection}")
            expected_auth = "bearer" if config.api == "responses" else "anthropic_api_key"
            if connection.auth.type != expected_auth:
                raise ValueError(f"model {model} {config.api} API requires {expected_auth} connection auth")
        return self

    def connection(self, model: str) -> ConnectionConfig:
        config = self.models.get(model)
        if config is None:
            raise ValueError(f"unknown model {model}")
        connection = self.connections.get(config.connection)
        if connection is None:
            raise ValueError(f"model {model} references unknown connection {config.connection}")
        return connection

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

    @model_validator(mode="after")
    def paired_service_credentials(self) -> Self:
        if (self.history_target is None) != (self.history_token_file is None):
            raise ValueError("history target and token file must be configured together")
        if (self.gateway_target is None) != (self.gateway_token_file is None):
            raise ValueError("gateway target and token file must be configured together")
        return self

    @model_validator(mode="after")
    def allowed_account_names(self) -> Self:
        if any(
            len(account.split("/")) != 2 or any(not part for part in account.split("/"))
            for account in self.allowed_accounts
        ):
            raise ValueError("allowed_accounts must use namespace/name")
        return self

    @field_serializer("allowed_accounts")
    def serialize_allowed_accounts(self, value: set[str]) -> list[str]:
        return sorted(value)
