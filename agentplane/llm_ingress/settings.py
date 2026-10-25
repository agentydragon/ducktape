"""Authoring schema for the Go LLM ingress deployment settings."""

from pathlib import Path
from typing import ClassVar

from pydantic import Field, SecretStr
from pydantic_settings import SettingsConfigDict

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
    kubeconfig: Path | None = Field(default=None, description="Kubeconfig to use; omit for in-cluster.")
