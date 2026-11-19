"""Typed JSON configuration schema for the central sampler Go service."""

from ipaddress import ip_address
from pathlib import Path
from typing import ClassVar, Literal, Self
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_serializer, field_validator, model_validator
from pydantic_settings import SettingsConfigDict

from agentplane.net.peertls.config import (
    CENTRAL_SAMPLER_URI,
    LLM_INGRESS_URI,
    SANDBOX_CONTROLLER_URI,
    SESSION_DISPATCHER_URI,
    TLS,
    ClientTLS,
    ServerTLSListener,
    ServiceSPIFFEID,
)

DEFAULT_EXECUTOR_INITIALIZE_TIMEOUT_SECONDS = 30
MAX_EXECUTOR_INITIALIZE_TIMEOUT_SECONDS = (2**63 - 1) // 1_000_000_000


class ActionsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    target: str = Field(min_length=1, pattern=r"\S")
    tls: TLS


class AuthConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # `agentplane_ingress` uses a projected workload token or the sampler's service SVID;
    # a Session executing as an Identity is sampled as a delegating actor for it (SPEC.md).
    type: Literal["bearer", "agentplane_ingress", "anthropic_api_key"]
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
        if self.type == "agentplane_ingress" and self.credential is None and self.credential_file is None:
            # The containing ConnectionConfig validates the certificate source.
            return self
        if (self.credential is None) == (self.credential_file is None):
            raise ValueError("configure exactly one of credential and credential_file")
        if self.credential is not None and not self.credential.get_secret_value():
            raise ValueError("credential must not be empty")
        return self


class ConnectionConfig(BaseModel):
    tls: ClientTLS | None = None
    model_config = ConfigDict(extra="forbid")

    base_url: str
    auth: AuthConfig

    @model_validator(mode="after")
    def tls_requires_https(self) -> Self:
        if self.tls is not None and urlsplit(self.base_url).scheme != "https":
            raise ValueError("connection tls requires an https base_url")
        return self

    @model_validator(mode="after")
    def ingress_authentication(self) -> Self:
        uses_svid = self.tls is not None and self.tls.uses_svid
        has_credential = self.auth.credential is not None or self.auth.credential_file is not None
        if self.auth.type != "agentplane_ingress":
            if uses_svid:
                raise ValueError("service SVID authentication is only supported for agentplane_ingress connections")
            return self
        if self.tls is not None and self.tls.server_uri != LLM_INGRESS_URI:
            raise ValueError("agentplane_ingress connections must verify the LLM ingress service")
        if self.tls is not None and uses_svid:
            self.tls.validate_caller("configured" if has_credential else None, CENTRAL_SAMPLER_URI)
        elif not has_credential:
            raise ValueError("agentplane_ingress requires a credential or central sampler service SVID")
        return self

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
    environment_namespace: str = Field(
        min_length=1,
        max_length=63,
        pattern=r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$",
        description="Namespace of executor environments.",
    )
    history_tls: ClientTLS | None = None
    gateway_tls: ClientTLS | None = None
    config_file_env: ClassVar[str] = "AGENTPLANE_CENTRAL_SAMPLER_CONFIG"
    model_config = SettingsConfigDict(extra="forbid", env_prefix="AGENTPLANE_CENTRAL_SAMPLER_")

    actions: ActionsConfig | None = None
    database_url: str | None = None
    health_listen: str | None = Field(
        default=None, description="Optional gRPC health-only plaintext listener; defaults to :8081."
    )
    listen_tls: ServerTLSListener = Field(
        description="The server-authenticated TLS listener for HarnessHost and SamplerAdmin RPCs."
    )
    connections: dict[str, ConnectionConfig] = Field(min_length=1)
    models: dict[str, ModelConfig] = Field(min_length=1)

    platform_instructions: str | None = Field(default=None, min_length=1)
    service_callers: list[ServiceSPIFFEID] = Field(
        exclude_if=lambda value: not value,
        default_factory=list,
        description="Exact service SPIFFE IDs admitted on the mTLS listener.",
    )
    admin_service_callers: list[ServiceSPIFFEID] = Field(
        exclude_if=lambda value: not value,
        default_factory=list,
        description="Exact service SPIFFE IDs admitted to SamplerAdmin RPCs.",
    )
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
    provider_response_header_timeout_seconds: int | None = Field(
        default=None,
        gt=0,
        le=MAX_EXECUTOR_INITIALIZE_TIMEOUT_SECONDS,
        description="Time allowed for provider request transmission through response headers.",
    )
    provider_idle_timeout_seconds: int | None = Field(
        default=None,
        gt=0,
        le=MAX_EXECUTOR_INITIALIZE_TIMEOUT_SECONDS,
        description="Time allowed between durable semantic provider events after response headers.",
    )
    turn_timeout_seconds: int | None = Field(
        default=None,
        gt=0,
        le=MAX_EXECUTOR_INITIALIZE_TIMEOUT_SECONDS,
        description="Optional elapsed turn limit; omitted means no maximum.",
    )
    setup_timeout_seconds: int | None = Field(
        default=None,
        gt=0,
        le=MAX_EXECUTOR_INITIALIZE_TIMEOUT_SECONDS,
        description="Maximum setup script duration; the service uses 3600 seconds when omitted.",
    )
    rpc_timeout_seconds: int = Field(gt=0)
    code_mode_observation_timeout_seconds: int | None = Field(
        default=None,
        gt=0,
        le=MAX_EXECUTOR_INITIALIZE_TIMEOUT_SECONDS,
        description="Maximum requested wait for a JavaScript observation; execution can continue after yielding.",
    )
    executor_initialize_timeout_seconds: int = Field(
        default=DEFAULT_EXECUTOR_INITIALIZE_TIMEOUT_SECONDS,
        gt=0,
        le=MAX_EXECUTOR_INITIALIZE_TIMEOUT_SECONDS,
        description="Time allowed for the executor OpenSession initialization handshake.",
    )
    executor_reconnect_timeout_seconds: int | None = Field(
        default=None,
        gt=0,
        le=MAX_EXECUTOR_INITIALIZE_TIMEOUT_SECONDS,
        description="Time allowed to resume a retained executor session during a connection outage.",
    )
    shutdown_drain_seconds: int = Field(
        default=40, gt=0, description="Wait for active effects to settle before canceling them during shutdown."
    )
    shutdown_budget_seconds: int = Field(
        default=55, gt=0, description="Maximum shutdown cleanup budget; must fit inside the Pod termination grace."
    )
    lease_seconds: int = Field(gt=30)
    auto_compact_tokens: int | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def validate_listeners(self) -> Self:
        health_listen = self.health_listen if self.health_listen is not None else ":8081"
        if self.health_listen is not None and not self.health_listen.strip():
            raise ValueError("health_listen must not be empty when configured")
        if health_listen == self.listen_tls.listen:
            raise ValueError("listen_tls.listen must differ from health_listen")
        return self

    @model_validator(mode="after")
    def observation_budget_fits_duration(self) -> Self:
        observation_seconds = self.code_mode_observation_timeout_seconds
        if observation_seconds is None:
            observation_seconds = 300
        if observation_seconds + self.rpc_timeout_seconds > MAX_EXECUTOR_INITIALIZE_TIMEOUT_SECONDS:
            raise ValueError("code-mode observation and RPC budgets must fit together in time.Duration")
        return self

    @field_validator("platform_instructions")
    @classmethod
    def nonempty_platform_instructions(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not value.strip():
            raise ValueError("platform_instructions must not be empty")
        return value.strip()

    @model_validator(mode="after")
    def validate_model_connections(self) -> Self:
        if self.shutdown_budget_seconds - self.shutdown_drain_seconds < 15:
            raise ValueError("shutdown budget must leave at least 15 seconds after the drain deadline")
        if any(not name for name in self.connections):
            raise ValueError("connection name must not be empty")
        for model, config in self.models.items():
            if not model:
                raise ValueError("model route must not be empty")
            connection = self.connections.get(config.connection)
            if connection is None:
                raise ValueError(f"model {model} references unknown connection {config.connection}")
            expected_auth = {"bearer", "agentplane_ingress"} if config.api == "responses" else {"anthropic_api_key"}
            if connection.auth.type not in expected_auth:
                raise ValueError(
                    f"model {model} {config.api} API requires {' or '.join(sorted(expected_auth))} connection auth"
                )
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

    @model_validator(mode="after")
    def paired_service_credentials(self) -> Self:
        for target, token, tls in (
            (self.history_target, self.history_token_file, self.history_tls),
            (self.gateway_target, self.gateway_token_file, self.gateway_tls),
        ):
            if target is None:
                if token is not None or tls is not None:
                    raise ValueError("service credentials require a target")
            elif tls is not None:
                tls.validate_caller(token, CENTRAL_SAMPLER_URI)
            elif token is None:
                raise ValueError("a service target requires a bearer token file or service SVID")
        return self

    @model_validator(mode="after")
    def gateway_tls_names_runtime_proxy_listener(self) -> Self:
        if self.gateway_tls is not None and self.gateway_tls.server_uri not in {
            SESSION_DISPATCHER_URI,
            SANDBOX_CONTROLLER_URI,
        }:
            raise ValueError("gateway_tls.server_uri must name the Gateway or Environments Runtime proxy listener")
        return self
