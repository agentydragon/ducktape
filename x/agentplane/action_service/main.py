"""Composition root for the independently deployable Agentplane Action Service."""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

import uvicorn
from kubernetes_asyncio import client as k8s_client, config as k8s_config
from kubernetes_asyncio.client import ApiClient, AuthenticationV1Api, CoreV1Api
from pydantic import Field
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict, YamlConfigSettingsSource

from x.agentplane.action_service.api import create_app
from x.agentplane.action_service.auth import (
    ConfiguredOperatorBearerAuthenticator,
    DisabledOperatorAuthenticator,
    OperatorAuthenticator,
)
from x.agentplane.action_service.catalog import ActionCatalog, ActionGroup
from x.agentplane.action_service.db import ActionStore, make_engine, make_sessionmaker, verify_schema
from x.agentplane.action_service.service import ActionService, EchoExecutor
from x.agentplane.sandbox_auth.http import SandboxPrincipalAuthenticator
from x.agentplane.sandbox_auth.principal import SandboxPrincipalResolver

# YamlConfigSettingsSource loads yaml lazily inside pydantic-settings; gazelle cannot see the dependency.
# gazelle:include_dep @pypi//pyyaml

logger = logging.getLogger(__name__)


class Settings(BaseSettings):
    """The service's configuration.

    Each field is a `--flag`, an `AGENTPLANE_ACTIONS_*` environment variable, and a key of the YAML
    file `AGENTPLANE_ACTIONS_CONFIG_FILE` names, in that order of precedence; the staging Deployment
    keeps the ActionGroup catalog in that file so backend/account changes need only a restart.
    """

    model_config = SettingsConfigDict(env_prefix="AGENTPLANE_ACTIONS_", cli_parse_args=True, cli_kebab_case=True)

    database_url: str = Field(description="Action Service-owned PostgreSQL database URL.")
    host: str = "127.0.0.1"
    port: int = 8080
    token_audience: str = "agentplane-egress"
    sandbox_namespaces: frozenset[str] = frozenset({"agentplane-staging"})
    operator_bearer_file: Path | None = None
    operator_subject: str = "configured-bff"
    action_groups: dict[str, ActionGroup] = Field(
        default_factory=dict, description="Reviewed ActionGroup catalog, keyed by stable namespaced group key."
    )

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
        if config_file := os.environ.get("AGENTPLANE_ACTIONS_CONFIG_FILE"):
            sources.append(YamlConfigSettingsSource(settings_cls, yaml_file=config_file))
        sources.append(file_secret_settings)
        return tuple(sources)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    asyncio.run(async_main(Settings()))


async def async_main(settings: Settings) -> None:
    engine = make_engine(settings.database_url)
    await verify_schema(engine)
    configuration = k8s_client.Configuration()
    k8s_config.load_incluster_config(client_configuration=configuration)
    catalog = ActionCatalog(groups=settings.action_groups)
    async with ApiClient(configuration=configuration) as api:
        service = ActionService(ActionStore(make_sessionmaker(engine)), EchoExecutor())
        recovered = await service.start()
        if recovered:
            logger.warning("marked %d in-flight executions unknown after restart", recovered)
        operator_authenticator: OperatorAuthenticator
        if settings.operator_bearer_file is None:
            operator_authenticator = DisabledOperatorAuthenticator()
            logger.info("operator/BFF API is disabled because no operator authenticator is configured")
        else:
            operator_authenticator = ConfiguredOperatorBearerAuthenticator.from_file(
                settings.operator_bearer_file, subject=settings.operator_subject
            )
        app = create_app(
            service,
            SandboxPrincipalAuthenticator(
                SandboxPrincipalResolver(
                    authentication=AuthenticationV1Api(api),
                    core_v1=CoreV1Api(api),
                    audience=settings.token_audience,
                    namespaces=settings.sandbox_namespaces,
                )
            ),
            operator_authenticator,
            catalog,
        )
        try:
            await uvicorn.Server(uvicorn.Config(app, host=settings.host, port=settings.port)).serve()
        finally:
            await service.close()
            await engine.dispose()


if __name__ == "__main__":
    main()
