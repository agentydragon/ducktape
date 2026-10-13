"""YAML deployment configuration; GitHub secrets are supplied through Secret-backed environment variables."""

from datetime import date
from pathlib import Path
from typing import Annotated, ClassVar

from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field, SecretStr
from pydantic_settings import SettingsConfigDict

from util.urls import HttpEndpointUrl
from util.yaml_file_settings import YamlFileSettings

TOKEN_AUDIENCE = "agentplane-notifications"
PositiveSeconds = Annotated[float, Field(gt=0, allow_inf_nan=False)]


class ActionsSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    url: HttpEndpointUrl = Field(description="Base URL of the Actions Service's canonical request/event read API.")
    token_file: Path = Field(
        description="Path to the projected ServiceAccount token for Actions; reread for each request to follow rotation."
    )
    request_timeout_s: PositiveSeconds = 5


class GitHubSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    api_url: HttpEndpointUrl = Field(
        default=AnyHttpUrl("https://api.github.com"),
        description="Trusted GitHub REST API base URL; receives App JWTs and installation tokens. Supports a path prefix.",
    )
    request_timeout_s: PositiveSeconds = 5
    api_version: date = Field(default=date(2022, 11, 28), description="GitHub REST API version header date.")

    app_id: int = Field(gt=0, description="Public numeric GitHub App ID; not a secret.")
    private_key: SecretStr = Field(
        min_length=1, description="PEM App private key, supplied through a Secret-backed environment variable."
    )
    webhook_secret: SecretStr = Field(
        min_length=16, description="Webhook HMAC signing secret, supplied through a Secret-backed environment variable."
    )
    freshness_seconds: int = Field(
        default=600,
        ge=60,
        le=3600,
        description="Maximum age of shared GitHub access validation and subject repair, in seconds.",
    )
    max_body_bytes: int = Field(
        default=1024 * 1024, ge=1024, le=25 * 1024 * 1024, description="Maximum raw webhook request body size in bytes."
    )
    webhook_concurrency: int = Field(
        default=8,
        ge=1,
        le=64,
        description="Maximum concurrent webhook requests per replica, held through durable commit; saturation returns 503.",
    )


class SandboxServiceSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    target: str = Field(description="Sandbox Service gRPC host:port, for Sandbox lookups.")
    request_timeout_s: PositiveSeconds = 5
    lifecycle_timeout_s: PositiveSeconds = 310
    token_file: Path = Field(
        description="Path to the rotating projected ServiceAccount token used to authenticate to Sandbox Service."
    )


class RunnerGatewaySettings(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    target: str = Field(description="Runner Gateway gRPC host:port for session access and runner commands.")
    control_target: str = Field(
        description="Each Sandbox's control Service, a template of `{sandbox}` and `{namespace}` such as "
        "`{sandbox}-control.{namespace}.svc:8080`. Lifecycle calls go there, to the replica holding the "
        "runner connection."
    )
    request_timeout_s: PositiveSeconds = 5
    lifecycle_timeout_s: PositiveSeconds = 310
    command_admission_timeout_s: PositiveSeconds = Field(
        default=20,
        description="Seconds to wait for the runner admission receipt via the Runner Gateway; keep above its "
        "runner_admission_ack_timeout_s budget.",
    )
    token_file: Path = Field(
        description="Path to the rotating projected ServiceAccount token used to authenticate to the Runner Gateway."
    )


class HistoryServiceSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    target: str = Field(
        description="History Service gRPC host:port; delivery reads the Session's runner entries there."
    )
    request_timeout_s: PositiveSeconds = 20
    token_file: Path = Field(
        description="Path to the rotating projected ServiceAccount token used to authenticate to the History Service."
    )


class NoticeDebounceSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    quiet_seconds: float = Field(
        default=2,
        ge=0,
        le=3600,
        description="Wait this long after the newest unannounced inbox entry before preparing a runner notice. Zero disables debounce; persistence and inbox reads are never delayed.",
    )
    max_wait_seconds: float = Field(
        default=10,
        gt=0,
        le=3600,
        description="Cap the debounce wait from the oldest unannounced inbox entry, even during continuous traffic. Does not bound runner outages or delivery retries.",
    )


class QuotaSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    inboxes_per_account: int = Field(
        default=64, ge=1, description="Maximum retained inboxes per owning ServiceAccount."
    )
    active_subscriptions_per_inbox: int = Field(
        default=64, ge=1, description="Maximum non-cancelled, unexpired subscriptions per inbox."
    )
    entries_per_inbox: int = Field(
        default=10_000, ge=1, description="Maximum distinct source-event identities over an inbox's lifetime."
    )


class Settings(YamlFileSettings):
    config_file_env: ClassVar[str] = "AGENTPLANE_NOTIFICATIONS_CONFIG_FILE"
    model_config = SettingsConfigDict(
        env_prefix="AGENTPLANE_NOTIFICATIONS_",
        env_nested_delimiter="__",
        extra="forbid",
        cli_parse_args=True,
        cli_kebab_case=True,
        hide_input_in_errors=True,
    )
    database_url: str = Field(description="PostgreSQL DSN supplied through a Secret-backed environment variable.")
    namespace: str = Field(description="Allowed caller ServiceAccount namespace and destination sandbox namespace.")
    actions: ActionsSettings
    sandbox_service: SandboxServiceSettings
    runner_gateway: RunnerGatewaySettings
    history_service: HistoryServiceSettings
    token_audience: str = Field(
        default=TOKEN_AUDIENCE, description="Audience required when TokenReview authenticates callers of this API."
    )
    operator_reader_account: str | None = Field(
        default=None,
        description="ServiceAccount name allowed to read UID-pinned operator diagnostics, not inbox payloads.",
    )
    quotas: QuotaSettings = Field(default_factory=QuotaSettings)
    notice_debounce: NoticeDebounceSettings = Field(default_factory=NoticeDebounceSettings)
    stale_inbox_confirmation_s: PositiveSeconds = Field(
        default=30, le=3600, description="Seconds between first and confirming unavailable UID-pinned Sandbox lookup."
    )
    host: str = "0.0.0.0"
    port: int = Field(default=8080, ge=1, le=65535)
    github: GitHubSettings | None = Field(
        default=None, description="GitHub source configuration. Disabled when absent from both YAML and environment."
    )
