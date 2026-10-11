"""Standalone notification API and workers. All dependencies are independently owned backends."""

import asyncio
from contextlib import AsyncExitStack

import httpx
import uvicorn
from kubernetes_asyncio import client as k8s_client
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from agentplane.history_service.client import HistoryServiceClient
from agentplane.notification_service.api import create_app
from agentplane.notification_service.service import Service
from agentplane.notification_service.settings import Settings
from agentplane.notification_service.sources.actions import Actions
from agentplane.notification_service.sources.github.client import GitHubClient
from agentplane.notification_service.sources.github.source import GitHub
from agentplane.notification_service.store import Store
from agentplane.sandbox_service.client import SandboxServiceClient
from agentplane.service_process import configure_logging, kubernetes_configuration
from agentplane.workload_auth.principal import WorkloadPrincipalResolver

# gazelle:include_dep @pypi//asyncpg


async def serve(settings: Settings) -> None:
    # Staging OOM: 22 notification sessions, 21 idle across two replicas.
    engine = create_async_engine(
        make_url(settings.database_url).set(drivername="postgresql+asyncpg"), pool_size=4, max_overflow=2
    )
    try:
        async with (
            k8s_client.ApiClient(await kubernetes_configuration(None)) as kube,
            httpx.AsyncClient(
                base_url=str(settings.actions.url), timeout=settings.actions.request_timeout_s, follow_redirects=False
            ) as http,
            AsyncExitStack() as github_stack,
            SandboxServiceClient(
                settings.sandbox_service.target,
                gateway_target=settings.sandbox_service.gateway_target,
                namespace=settings.namespace,
                token_file=settings.sandbox_service.token_file,
                gateway_token_file=settings.sandbox_service.gateway_token_file,
                request_timeout_s=settings.sandbox_service.request_timeout_s,
                lifecycle_timeout_s=settings.sandbox_service.lifecycle_timeout_s,
                command_admission_timeout_s=settings.sandbox_service.command_admission_timeout_s,
                control_target=settings.sandbox_service.control_target,
                channel_options=settings.sandbox_service.grpc_channel_options,
            ) as sandboxes,
            HistoryServiceClient(
                settings.history_service.target,
                token_file=settings.history_service.token_file,
                request_timeout_s=settings.history_service.request_timeout_s,
                channel_options=settings.history_service.grpc_channel_options,
            ) as history,
        ):
            principals = WorkloadPrincipalResolver(
                authentication=k8s_client.AuthenticationV1Api(kube),
                audience=settings.token_audience,
                allowed_service_account_namespaces={settings.namespace},
            )
            github = None
            if settings.github is not None:
                client = await github_stack.enter_async_context(GitHubClient.open(settings.github))
                github = GitHub(client)
            app = create_app(
                Service(
                    Store(engine, quotas=settings.quotas),
                    Actions(http, settings.actions.token_file),
                    sandboxes,
                    github,
                    history=history,
                    notice_debounce=settings.notice_debounce,
                    stale_confirmation_s=settings.stale_inbox_confirmation_s,
                    operator_reader_account=settings.operator_reader_account,
                ),
                principals,
            )
            await uvicorn.Server(uvicorn.Config(app, host=settings.host, port=settings.port)).serve()
    finally:
        await engine.dispose()


def main() -> None:
    configure_logging()
    asyncio.run(serve(Settings()))


if __name__ == "__main__":
    main()
