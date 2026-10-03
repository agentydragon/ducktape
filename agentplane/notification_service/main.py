"""Standalone notification API and workers. All dependencies are independently owned backends."""

import asyncio
from pathlib import Path

import httpx
import uvicorn
from kubernetes_asyncio import client as k8s_client, config as k8s_config
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from agentplane.notification_service.actions import Actions
from agentplane.notification_service.api import create_app
from agentplane.notification_service.service import Service
from agentplane.notification_service.store import Store
from agentplane.sandbox_service.client import SandboxServiceClient
from agentplane.workload_auth.principal import WorkloadPrincipalResolver

# gazelle:include_dep @pypi//asyncpg


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="AGENTPLANE_NOTIFICATIONS_", cli_parse_args=True, cli_kebab_case=True, hide_input_in_errors=True
    )
    database_url: str
    namespace: str
    actions_url: str
    actions_token_file: Path
    sandbox_service_target: str
    sandbox_service_token_file: Path
    token_audience: str = "agentplane-egress"
    host: str = "0.0.0.0"
    port: int = Field(default=8080, ge=1, le=65535)


async def serve(settings: Settings) -> None:
    k8s_config.load_incluster_config()
    engine = create_async_engine(
        make_url(settings.database_url).set(drivername="postgresql+asyncpg"), pool_size=10, max_overflow=5
    )
    sandboxes = SandboxServiceClient(
        settings.sandbox_service_target,
        namespace=settings.namespace,
        token_file=settings.sandbox_service_token_file,
        request_timeout_s=5,
    )
    try:
        async with (
            k8s_client.ApiClient() as kube,
            httpx.AsyncClient(base_url=settings.actions_url, timeout=5, follow_redirects=False) as http,
        ):
            principals = WorkloadPrincipalResolver(
                authentication=k8s_client.AuthenticationV1Api(kube),
                audience=settings.token_audience,
                allowed_service_account_namespaces={settings.namespace},
            )
            app = create_app(Service(Store(engine), Actions(http, settings.actions_token_file), sandboxes), principals)
            await uvicorn.Server(uvicorn.Config(app, host=settings.host, port=settings.port)).serve()
    finally:
        await sandboxes.close()
        await engine.dispose()


def main() -> None:
    asyncio.run(serve(Settings()))


if __name__ == "__main__":
    main()
