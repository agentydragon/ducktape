"""Run the Agentplane LLM workload ingress."""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Any

import httpx
import uvicorn
from kubernetes_asyncio import client as k8s_client, config as k8s_config
from kubernetes_asyncio.client import ApiClient, AuthenticationV1Api
from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict, YamlConfigSettingsSource

from x.agentplane.llm_ingress.app import IngressResources, create_app
from x.agentplane.workload_auth.http import WorkloadPrincipalAuthenticator
from x.agentplane.workload_auth.principal import WorkloadPrincipalResolver

# YamlConfigSettingsSource loads yaml lazily inside pydantic-settings; gazelle cannot see the dependency.
# gazelle:include_dep @pypi//pyyaml

logger = logging.getLogger(__name__)


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
    litellm_url: str = Field(description="Internal LiteLLM base URL.")
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

    def __init__(self, **values: Any) -> None:
        super().__init__(**values)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    # TokenReview responses echo the submitted bearer. The generated client logs full response
    # bodies at DEBUG, so pin its wire logger above that level even if a future root config is noisy.
    logging.getLogger("kubernetes_asyncio.client.rest").setLevel(logging.INFO)
    asyncio.run(async_main(Settings()))


async def async_main(settings: Settings) -> None:
    configuration = k8s_client.Configuration()
    if settings.kubeconfig is None:
        k8s_config.load_incluster_config(client_configuration=configuration)
    else:
        await k8s_config.load_kube_config(config_file=str(settings.kubeconfig), client_configuration=configuration)
    timeout = httpx.Timeout(connect=5, read=None, write=60, pool=5)
    async with (
        ApiClient(configuration=configuration) as api,
        httpx.AsyncClient(base_url=settings.litellm_url, timeout=timeout) as backend,
    ):
        resolver = WorkloadPrincipalResolver(
            authentication=AuthenticationV1Api(api),
            audience=settings.token_audience,
            allowed_service_account_namespaces=settings.allowed_service_account_namespaces,
        )
        app = create_app(
            IngressResources(
                authenticate=WorkloadPrincipalAuthenticator(resolver),
                backend=backend,
                litellm_key=settings.litellm_key.get_secret_value(),
            )
        )
        logger.info("forwarding authenticated Sandbox model traffic to %s", settings.litellm_url)
        await uvicorn.Server(uvicorn.Config(app, host=settings.host, port=settings.port, access_log=False)).serve()


if __name__ == "__main__":
    main()
