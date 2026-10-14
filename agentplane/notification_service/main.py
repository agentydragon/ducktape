"""Standalone notification API and workers. All dependencies are independently owned backends."""

import asyncio
from contextlib import AsyncExitStack
from typing import cast

import httpx
import uvicorn
from kubernetes_asyncio import client as k8s_client
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from agentplane.grpc_calls import dial
from agentplane.history_service.client import HistoryServiceClient
from agentplane.notification_service.api import create_app
from agentplane.notification_service.dispatcher_client import SessionDispatcherClient
from agentplane.notification_service.service import Service
from agentplane.notification_service.settings import Settings
from agentplane.notification_service.sources.actions import ActionServiceClient
from agentplane.notification_service.sources.github.client import GitHubClient
from agentplane.notification_service.sources.github.source import GitHub
from agentplane.notification_service.store import Store
from agentplane.runner_gateway.client import RunnerGatewayClient, SandboxControlClient
from agentplane.sandboxes.reader import SandboxReader
from agentplane.service_process import configure_logging, kubernetes_configuration
from agentplane.workload_auth.principal import WorkloadPrincipalResolver
from util.kubernetes import CustomObjectsClient

# gazelle:include_dep @pypi//asyncpg


async def serve(settings: Settings) -> None:
    async with AsyncExitStack() as stack:
        # Staging OOM: 22 notification sessions, 21 idle across two replicas.
        engine = create_async_engine(
            make_url(settings.database_url).set(drivername="postgresql+asyncpg"), pool_size=4, max_overflow=2
        )
        stack.push_async_callback(engine.dispose)
        dispatcher_channel = dial(settings.session_dispatcher.target)
        stack.push_async_callback(dispatcher_channel.close)
        dispatcher = SessionDispatcherClient(
            dispatcher_channel,
            token_file=settings.session_dispatcher.token_file,
            request_timeout_s=settings.session_dispatcher.request_timeout_s,
        )
        async with (
            k8s_client.ApiClient(await kubernetes_configuration(None)) as kube,
            httpx.AsyncClient(
                base_url=str(settings.actions.url), timeout=settings.actions.request_timeout_s, follow_redirects=False
            ) as http,
            AsyncExitStack() as github_stack,
            dial(settings.runner_gateway.target) as gateway_channel,
            HistoryServiceClient(
                settings.history_service.target,
                token_file=settings.history_service.token_file,
                request_timeout_s=settings.history_service.request_timeout_s,
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
                    ActionServiceClient(http, settings.actions.token_file),
                    SandboxReader(
                        namespace=settings.namespace,
                        custom_objects=cast(CustomObjectsClient, k8s_client.CustomObjectsApi(kube)),
                        core_v1=k8s_client.CoreV1Api(kube),
                    ),
                    github,
                    gateway=RunnerGatewayClient(
                        gateway_channel,
                        token_file=settings.runner_gateway.token_file,
                        command_admission_timeout_s=settings.runner_gateway.command_admission_timeout_s,
                        request_timeout_s=settings.runner_gateway.request_timeout_s,
                        control=SandboxControlClient(
                            control_target=settings.runner_gateway.control_target,
                            token_file=settings.runner_gateway.token_file,
                            request_timeout_s=settings.runner_gateway.request_timeout_s,
                            lifecycle_timeout_s=settings.runner_gateway.lifecycle_timeout_s,
                        ),
                    ),
                    history=history,
                    dispatcher=dispatcher,
                    notice_debounce=settings.notice_debounce,
                    stale_confirmation_s=settings.stale_inbox_confirmation_s,
                    operator_reader_account=settings.operator_reader_account,
                ),
                principals,
            )
            await uvicorn.Server(uvicorn.Config(app, host=settings.host, port=settings.port)).serve()


def main() -> None:
    settings = Settings()
    configure_logging(settings.log_levels)
    asyncio.run(serve(settings))


if __name__ == "__main__":
    main()
