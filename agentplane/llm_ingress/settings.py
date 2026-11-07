"""Authoring schema for the Go LLM ingress deployment settings."""

from pathlib import Path
from typing import ClassVar, Self

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import SettingsConfigDict

from agentplane.environment_runtime_transport.config import TLS
from agentplane.llm_ingress.models import AttributionConfig, ModelConfig, default_attribution
from agentplane.log_levels import LogLevels
from util.urls import HttpEndpointUrl
from util.yaml_file_settings import YamlFileSettings


class Settings(YamlFileSettings):
    """Fields are YAML keys; scalar fields also accept `AGENTPLANE_LLM_INGRESS_*` environment overrides."""

    config_file_env: ClassVar[str] = "AGENTPLANE_LLM_INGRESS_CONFIG_FILE"
    model_config = SettingsConfigDict(env_prefix="AGENTPLANE_LLM_INGRESS_")

    allowed_service_account_namespaces: frozenset[str] = Field(
        min_length=1,
        description="Every namespace whose ServiceAccounts may authenticate here. The central proxy is the "
        "only client, so this must admit at least what the proxy's own allowlist does: a workload it "
        "authenticated and sent on is refused here if its namespace is missing.",
    )
    central_sampler_accounts: frozenset[str] = Field(
        default_factory=frozenset,
        description="Trusted namespace/name sampler producers allowed to attribute public Session IDs.",
    )
    token_audience: str = Field(min_length=1, description="Accepted projected-token audience.")
    log_llm_requests: bool = Field(
        default=False,
        description=(
            "Log full LLM request bodies and streamed response chunks. These logs can contain prompts, "
            "reasoning, generated text, and tool arguments."
        ),
    )
    models: list[ModelConfig] = Field(
        default_factory=list, description="Per-route client configuration exposed to authenticated workloads."
    )
    attribution: AttributionConfig = Field(
        default_factory=default_attribution,
        description="Literal JSON-header and W3C baggage keys mapped to verified Kubernetes principal fields.",
    )

    codex_model_catalog_file: Path | None = Field(
        default=None,
        description=(
            "JSON file with a `models` list, served to Codex at `/agentplane/codex/models`; its hash is sent as "
            "`X-Models-Etag` on every Responses reply, so a running Codex refetches an edited catalog."
        ),
    )
    litellm_url: HttpEndpointUrl = Field(description="Internal LiteLLM base URL.")
    litellm_key: SecretStr = Field(description="The one server-held LiteLLM virtual key.")
    log_levels: LogLevels
    host: str = Field(default="0.0.0.0", description="Listener bind address.")
    port: int = Field(default=8080, description="Listener port.")
    grpc_port: int = Field(default=8081, description="Native gRPC listener port.")
    workload_tls: TLS | None = Field(
        default=None, description="Serving certificate and client trust roots for the Sandbox SVID listener."
    )
    workload_listen_address: str | None = Field(
        default=None, min_length=1, description="Address for the separate Sandbox SVID listener."
    )
    kubeconfig: Path | None = Field(default=None, description="Kubeconfig to use; omit for in-cluster.")

    @model_validator(mode="after")
    def workload_listener_pair(self) -> Self:
        if (self.workload_tls is None) != (self.workload_listen_address is None):
            raise ValueError("workload_tls and workload_listen_address must be configured together")
        return self
