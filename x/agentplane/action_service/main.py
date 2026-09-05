"""Composition root for the independently deployable Agentplane Action Service."""

from __future__ import annotations

import asyncio
import logging

import uvicorn
from kubernetes_asyncio import client as k8s_client, config as k8s_config
from kubernetes_asyncio.client import ApiClient, AuthenticationV1Api
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from x.agentplane.action_service.api import create_app
from x.agentplane.action_service.auth import KubernetesTokenAuthenticator
from x.agentplane.action_service.db import ActionStore, make_engine, make_sessionmaker, verify_schema
from x.agentplane.action_service.service import ActionService, EchoExecutor

logger = logging.getLogger(__name__)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AGENTPLANE_ACTIONS_", cli_parse_args=True, cli_kebab_case=True)

    database_url: str = Field(description="Action Service-owned PostgreSQL database URL.")
    host: str = "127.0.0.1"
    port: int = 8080
    token_audience: str = "agentplane-actions"
    caller_subjects: frozenset[str] = frozenset()
    operator_subjects: frozenset[str] = frozenset()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    asyncio.run(async_main(Settings()))


async def async_main(settings: Settings) -> None:
    engine = make_engine(settings.database_url)
    await verify_schema(engine)
    configuration = k8s_client.Configuration()
    k8s_config.load_incluster_config(client_configuration=configuration)
    async with ApiClient(configuration=configuration) as api:
        service = ActionService(ActionStore(make_sessionmaker(engine)), EchoExecutor())
        recovered = await service.start()
        if recovered:
            logger.warning("marked %d in-flight executions unknown after restart", recovered)
        app = create_app(
            service,
            KubernetesTokenAuthenticator(
                AuthenticationV1Api(api),
                audience=settings.token_audience,
                caller_subjects=settings.caller_subjects,
                operator_subjects=settings.operator_subjects,
            ),
        )
        try:
            await uvicorn.Server(uvicorn.Config(app, host=settings.host, port=settings.port)).serve()
        finally:
            await service.close()
            await engine.dispose()


if __name__ == "__main__":
    main()
