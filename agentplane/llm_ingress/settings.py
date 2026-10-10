"""Deployment settings for the authenticated LLM ingress."""

import os
from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict, YamlConfigSettingsSource

from agentplane.llm_ingress.models import ModelConfig
from util.urls import HttpEndpointUrl

# YamlConfigSettingsSource loads yaml lazily inside pydantic-settings; gazelle cannot see the dependency.
# gazelle:include_dep @pypi//pyyaml

# Names the YAML settings file a deployment mounts; not a field, so not a flag.
CONFIG_FILE_ENV = "AGENTPLANE_LLM_INGRESS_CONFIG_FILE"


class Settings(BaseSettings):
    """Each field is a flag and an `AGENTPLANE_LLM_INGRESS_*` environment variable."""

    model_config = SettingsConfigDict(env_prefix="AGENTPLANE_LLM_INGRESS_", cli_parse_args=True, cli_kebab_case=True)

    allowed_service_account_namespaces: frozenset[str] = Field(
        min_length=1,
        description="Every namespace whose ServiceAccounts may authenticate here. The central proxy is the "
        "only client, so this must admit at least what the proxy's own allowlist does: a workload it "
        "authenticated and sent on is refused here if its namespace is missing.",
    )
    token_audience: str = Field(default="agentplane-egress", description="Accepted projected-token audience.")
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
    litellm_url: HttpEndpointUrl = Field(description="Internal LiteLLM base URL.")
    litellm_key: SecretStr = Field(description="The one server-held LiteLLM virtual key.")
    host: str = Field(default="0.0.0.0", description="Listener bind address.")
    port: int = Field(default=8080, description="Listener port.")
    kubeconfig: Path | None = Field(default=None, description="Kubeconfig to use; omit for in-cluster.")

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        sources: list[PydanticBaseSettingsSource] = [init_settings, env_settings, dotenv_settings]
        if config_file := os.environ.get(CONFIG_FILE_ENV):
            # pydantic-settings silently ignores absent YAML files. An explicit deployment binding
            # must never turn into a healthy service running on defaults.
            if not Path(config_file).is_file():
                raise ValueError("configured LLM ingress settings file is not a regular file")
            sources.append(YamlConfigSettingsSource(settings_cls, yaml_file=config_file))
        sources.append(file_secret_settings)
        return tuple(sources)
