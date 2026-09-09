"""Composition root for the independently deployable Agentplane Action Service."""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import AsyncExitStack
from pathlib import Path

import uvicorn
from kubernetes_asyncio import client as k8s_client, config as k8s_config
from kubernetes_asyncio.client import ApiClient, AuthenticationV1Api, CoreV1Api
from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict, YamlConfigSettingsSource

from x.agentplane.action_service.api import create_app
from x.agentplane.action_service.auth import (
    ConfiguredOperatorBearerAuthenticator,
    DisabledOperatorAuthenticator,
    OperatorAuthenticator,
)
from x.agentplane.action_service.catalog import ActionCatalog, ActionGroup, Key
from x.agentplane.action_service.connections import ConnectionAuthority, Identity
from x.agentplane.action_service.db import ActionStore, make_engine, make_sessionmaker, verify_schema
from x.agentplane.action_service.fixture_policy import FixtureAutoAllow, FixtureDecisionProvider
from x.agentplane.action_service.operator_oidc import OidcOperatorAuthenticator, OperatorOidcSettings
from x.agentplane.action_service.runtime import running_executor
from x.agentplane.action_service.service import ActionService
from x.agentplane.action_service.updates import ActionUpdates
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

    model_config = SettingsConfigDict(
        env_prefix="AGENTPLANE_ACTIONS_", cli_parse_args=True, cli_kebab_case=True, hide_input_in_errors=True
    )

    database_url: str = Field(description="Action Service-owned PostgreSQL database URL.")
    host: str = "127.0.0.1"
    port: int = 8080
    token_audience: str = "agentplane-egress"
    allowed_service_account_namespaces: frozenset[str] = Field(
        default=frozenset({"agentplane-staging"}),
        description="Kubernetes namespaces whose ServiceAccounts may authenticate sandbox callers; does not grant Action approval.",
    )
    operator_bearer_file: Path | None = None
    operator_oidc: OperatorOidcSettings | None = None
    operator_subject: str = "configured-bff"
    action_groups: dict[Key, ActionGroup] = Field(
        default_factory=dict, description="Reviewed ActionGroup catalog, keyed by stable namespaced group key."
    )
    identities: dict[Key, Identity] = Field(
        default_factory=dict,
        description="Configured external caller Identities; runtime Connections bind to these keys.",
    )

    fixture_auto_allow: FixtureAutoAllow | None = Field(
        default=None,
        description="Opt in to auto-allow only bounded echo(message) on a reviewed credentialless MCP group.",
    )

    @model_validator(mode="after")
    def one_operator_authority(self) -> Settings:
        if self.operator_oidc is not None and self.operator_bearer_file is not None:
            raise ValueError("configure operator_oidc or legacy operator_bearer_file, never both")
        return self

    def decision_providers(self, catalog: ActionCatalog) -> list[FixtureDecisionProvider]:
        if self.fixture_auto_allow is None:
            return []
        return [
            FixtureDecisionProvider(
                self.fixture_auto_allow,
                catalog,
                allowed_service_account_namespaces=self.allowed_service_account_namespaces,
            )
        ]

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
            # pydantic-settings silently ignores absent YAML files. An explicit deployment
            # binding must never turn into a healthy service with an empty catalog.
            if not Path(config_file).is_file():
                raise ValueError("configured Action Service settings file is not a regular file")
            sources.append(YamlConfigSettingsSource(settings_cls, yaml_file=config_file))
        sources.append(file_secret_settings)
        return tuple(sources)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    asyncio.run(async_main(Settings()))


async def async_main(settings: Settings) -> None:
    engine = make_engine(settings.database_url)
    async with AsyncExitStack() as stack:
        stack.push_async_callback(engine.dispose)
        await verify_schema(engine)
        configuration = k8s_client.Configuration()
        k8s_config.load_incluster_config(client_configuration=configuration)
        catalog = ActionCatalog(groups=settings.action_groups)
        providers = settings.decision_providers(catalog)
        api = await stack.enter_async_context(ApiClient(configuration=configuration))
        executors = await stack.enter_async_context(running_executor(catalog))
        connections = ConnectionAuthority(make_sessionmaker(engine), settings.identities)
        service = ActionService(
            ActionStore(make_sessionmaker(engine), external_grants=connections), catalog, executors, providers=providers
        )
        # Stop dispatch/lease tasks before closing the adapters, including failed service startup.
        stack.push_async_callback(service.close)
        await service.start()
        operator_authenticator: OperatorAuthenticator
        if settings.operator_oidc is not None:
            operator_authenticator = OidcOperatorAuthenticator(settings.operator_oidc)
        elif settings.operator_bearer_file is None:
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
                    allowed_service_account_namespaces=settings.allowed_service_account_namespaces,
                )
            ),
            operator_authenticator,
            catalog,
            connections=connections,
            updates=ActionUpdates(settings.database_url),
        )
        await uvicorn.Server(uvicorn.Config(app, host=settings.host, port=settings.port)).serve()


if __name__ == "__main__":
    main()
