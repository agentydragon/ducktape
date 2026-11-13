"""Authoring schema for the Go LLM ingress deployment settings."""

from pathlib import Path
from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field, field_serializer, model_validator

from agentplane.identity.auth.listener import Listener
from agentplane.llm_ingress.models import AttributionConfig, ModelConfig, default_attribution
from agentplane.log_levels import LogLevels
from util.urls import HttpEndpointUrl


class Settings(BaseModel):
    """Static deployment settings rendered to YAML, with one explicit runtime Secret input."""

    config_file_env: ClassVar[str] = "AGENTPLANE_LLM_INGRESS_CONFIG_FILE"
    litellm_key_env: ClassVar[str] = "AGENTPLANE_LLM_INGRESS_LITELLM_KEY"
    model_config = ConfigDict(extra="forbid")

    allowed_service_account_namespaces: frozenset[str] = Field(
        min_length=1,
        description="Namespaces whose Sandbox SVIDs or backend ServiceAccount bearers may authenticate here.",
    )

    @field_serializer("allowed_service_account_namespaces")
    def sort_allowed_service_account_namespaces(self, value: frozenset[str]) -> list[str]:
        return sorted(value)

    policy_namespace: str = Field(
        min_length=1,
        description=(
            "Namespace holding the Identities a Sandbox SVID may name. "
            "Every attribution output must then carry the `identity` claim."
        ),
    )
    central_sampler_accounts: frozenset[str] = Field(
        default_factory=frozenset,
        description="Trusted namespace/name sampler producers allowed to attribute public Session IDs.",
    )

    @field_serializer("central_sampler_accounts")
    def sort_central_sampler_accounts(self, value: frozenset[str]) -> list[str]:
        return sorted(value)

    central_sampler_identity_callers: bool = Field(
        default=False, description="Whether trusted central samplers may act as delegating actors for an Identity."
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
    log_levels: LogLevels
    rpc: Listener
    kubeconfig: Path | None = Field(default=None, description="Kubeconfig to use; omit for in-cluster.")

    @model_validator(mode="after")
    def validate_identity_attribution(self) -> Settings:
        if not self.attribution.names_identity():
            raise ValueError("policy_namespace requires every attribution output to carry the identity claim")
        return self
