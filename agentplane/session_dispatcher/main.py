"""Session Dispatcher entry point: stores commands for Sessions and delivers them to runners."""

import asyncio
from contextlib import AsyncExitStack
from typing import cast

import grpc
from kubernetes_asyncio import client as k8s_client
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from agentplane.grpc_serving import add_health, serving
from agentplane.sandboxes.inventory import SandboxInventory
from agentplane.service_process import configure_logging, kubernetes_configuration, until_terminated
from agentplane.session_dispatcher.delivery import Delivery
from agentplane.session_dispatcher.ended_dispatches import EndedDispatches
from agentplane.session_dispatcher.grpc_api import Resources, add_service
from agentplane.session_dispatcher.ownership import SandboxOwnershipLeases
from agentplane.session_dispatcher.runner_client import RunnerClient
from agentplane.session_dispatcher.sandboxes import SandboxWatch
from agentplane.session_dispatcher.settings import Settings
from agentplane.session_dispatcher.store import DispatchStore
from agentplane.session_dispatcher.terminal_broadcast import TerminalBroadcast
from agentplane.workload_auth.principal import WorkloadPrincipalResolver
from util.kubernetes import CustomObjectsClient

# The asyncpg dialect is loaded by SQLAlchemy from the configured driver name.
# gazelle:include_dep @pypi//asyncpg


async def serve(settings: Settings) -> None:
    async with AsyncExitStack() as stack:
        engine = create_async_engine(
            make_url(settings.database_url).set(drivername="postgresql+asyncpg"), pool_size=6, max_overflow=2
        )
        stack.push_async_callback(engine.dispose)
        ended = EndedDispatches()
        terminal_broadcast = TerminalBroadcast(engine.url, ended)
        store = DispatchStore(engine, queue_cap=settings.max_queued_per_session, terminal_broadcast=terminal_broadcast)
        async with (
            k8s_client.ApiClient(await kubernetes_configuration(settings.kubeconfig)) as api,
            store.wakeups.listener.listen(),
            terminal_broadcast.listener.listen(),
        ):
            custom_objects = cast(CustomObjectsClient, k8s_client.CustomObjectsApi(api))
            inventory = SandboxInventory(
                namespace=settings.namespace, custom_objects=custom_objects, core_v1=k8s_client.CoreV1Api(api)
            )
            sandboxes = SandboxWatch(
                custom_objects, namespace=settings.namespace, resync_seconds=settings.sandbox_resync_seconds
            )
            ownership = SandboxOwnershipLeases(k8s_client.CoordinationV1Api(api), store, namespace=settings.namespace)
            runner = RunnerClient(
                control_target=settings.runner_control_target,
                namespace=settings.namespace,
                token_file=settings.runner_token_file,
                request_timeout_s=settings.runner_request_timeout_s,
            )
            server = grpc.aio.server()
            add_health(server)
            add_service(
                Resources(
                    principals=WorkloadPrincipalResolver(
                        authentication=k8s_client.AuthenticationV1Api(api),
                        audience=settings.token_audience,
                        allowed_service_account_namespaces={account.namespace for account in settings.caller_accounts},
                    ),
                    caller_accounts=settings.caller_accounts,
                    store=store,
                    sandboxes=sandboxes,
                    runner=runner,
                    ended=ended,
                    request_timeout_s=settings.request_timeout_s,
                    watch_lease_s=settings.watch_lease_s,
                    wake_caller_accounts=settings.wake_caller_accounts,
                ),
                server,
            )
            # A worker's failure ends the process: a Dispatcher that accepts commands and delivers
            # none is worse than a restart.
            async with asyncio.TaskGroup() as workers:
                watching = workers.create_task(sandboxes.run(), name="sandbox-watch")
                delivering = workers.create_task(
                    Delivery(store, sandboxes, runner, ended, inventory, ownership).run(), name="delivery"
                )
                async with serving(server, f"{settings.host}:{settings.port}", grace_s=5):
                    await until_terminated()
                watching.cancel()
                delivering.cancel()


def main() -> None:
    settings = Settings()
    configure_logging(settings.log_levels)
    asyncio.run(serve(settings))


if __name__ == "__main__":
    main()
