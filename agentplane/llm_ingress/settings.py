"""Authoring schema for the Go LLM ingress deployment settings."""

from pathlib import Path
from typing import ClassVar

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import SettingsConfigDict

from agentplane.llm_ingress.models import AttributionConfig, ModelConfig, default_attribution
from agentplane.log_levels import LogLevels
from agentplane.workload_auth.listener import Listener
from util.urls import HttpEndpointUrl
from util.yaml_file_settings import YamlFileSettings


class Settings(YamlFileSettings):
    """Fields are YAML keys; scalar fields also accept `AGENTPLANE_LLM_INGRESS_*` environment overrides."""

    config_file_env: ClassVar[str] = "AGENTPLANE_LLM_INGRESS_CONFIG_FILE"
    model_config = SettingsConfigDict(env_prefix="AGENTPLANE_LLM_INGRESS_")

    allowed_service_account_namespaces: frozenset[str] = Field(
        min_length=1,
        description="Namespaces whose Sandbox SVIDs or backend ServiceAccount bearers may authenticate here.",
    )
    # CLEANUP(added 2026-11-10): make it required once SANDBOX_IDENTITY_SPEC lands
    #   (agentplane/plans/task_dag.md): a switched Sandbox must reach this service on day one.
    policy_namespace: str | None = Field(
        default=None,
        min_length=1,
        description=(
            "Namespace holding the Identities a Sandbox SVID may name; absent refuses Identity callers. "
            "Every attribution output must then carry the `identity` claim."
        ),
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
        description="Literal JSON-header and W3C baggage keys mapped to verified caller fields.",
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
    rpc: Listener
    kubeconfig: Path | None = Field(default=None, description="Kubeconfig to use; omit for in-cluster.")

    @model_validator(mode="after")
    def validate_identity_attribution(self) -> Settings:
        if self.policy_namespace is not None and not self.attribution.names_identity():
            raise ValueError("policy_namespace requires every attribution output to carry the identity claim")
        return self
