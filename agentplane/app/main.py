"""Serve the Agentplane app over one namespace's sandbox inventory."""

from __future__ import annotations

# Generated service annotations need the grpcio typing dependency.
# gazelle:include_dep @pypi//grpcio
# Generated protobuf annotations need the protobuf typing dependency.
# gazelle:include_dep @pypi//protobuf
import asyncio
import socket
from contextlib import AsyncExitStack
from typing import cast

import anyio
import httpx
import uvicorn
from kubernetes_asyncio.client import ApiClient, AuthenticationV1Api, CoreV1Api, CustomObjectsApi
from sqlalchemy.ext.asyncio import AsyncEngine

from agentplane.action_service import (
    action_service_pb2_grpc,
    operator_action_service_pb2_grpc,
    web_push_service_pb2_grpc,
)
from agentplane.app.action_federation import FederatedOperatorActions
from agentplane.app.action_policy import ActionPolicyInventory
from agentplane.app.api import create_app
from agentplane.app.database import connect
from agentplane.app.database_updates import Channel, create_database_updates
from agentplane.app.egress_access import EgressAccess
from agentplane.app.electric import ElectricProxy
from agentplane.app.grpc_stream import OperatorGrpcClient
from agentplane.app.identity import TokenReviewer
from agentplane.app.live import LiveIndex, watch_for
from agentplane.app.notifications_operator_client import NotificationsOperatorClient
from agentplane.app.oidc import OIDCSettings
from agentplane.app.operator_sessions import OperatorSessionStore
from agentplane.app.presets import PresetCatalog
from agentplane.app.sandbox_access import SandboxAccess
from agentplane.app.settings import Settings
from agentplane.app.shutdown import Drain, drain_of
from agentplane.app.system_status import SystemStatusChecker, grpc_health_probe, http_health_probe
from agentplane.app.threads.bridge import RunnerBridge
from agentplane.app.threads.dispatcher_client import SessionDispatcherClient
from agentplane.app.threads.events.event_log import EventLogStore
from agentplane.app.threads.history_watch import HistoryWatch, SessionOpenings
from agentplane.app.threads.runner_directory import RunnerDirectory
from agentplane.app.threads.store import ThreadStore
from agentplane.app.threads.summaries import Summaries
from agentplane.app.threads.view.content import ContentStore
from agentplane.background_task import background
from agentplane.egress.decisions_client import DecisionsClient
from agentplane.grpc.calls import dial
from agentplane.history_service.client import HistoryServiceClient
from agentplane.kubernetes_watch import STALE_AFTER_CYCLES, Freshness
from agentplane.mcp_edge import connection_service_pb2_grpc
from agentplane.postgres.wakeups import WakeupListener
from agentplane.runner_gateway.client import SandboxControlClient
from agentplane.sandboxes.inventory import SandboxInventory
from agentplane.service_process import configure_logging, kubernetes_configuration
from agentplane.subjects import ServiceAccountRef
from agentplane.tracing import configure_tracing
from util.kubernetes import CustomObjectsClient

# Each System page probe's budget; the probes run together, so the page answers within it.
SYSTEM_STATUS_TIMEOUT_S = 5


class AppServer(uvicorn.Server):
    """Uvicorn, with the app's drain begun as its shutdown starts."""

    def __init__(self, config: uvicorn.Config, drain: Drain) -> None:
        super().__init__(config)
        self._drain = drain

    async def shutdown(self, sockets: list[socket.socket] | None = None) -> None:
        # Before Uvicorn waits on what is open: the streams end now rather than at its budget, and
        # /readyz fails. Here rather than in `handle_exit`, whose signal context can interrupt the
        # loop mid-`Event.wait` and lose the waiter; the tick between the signal and this is 0.1s.
        self._drain.begin()
        await super().shutdown(sockets)


def main() -> None:
    settings = Settings()
    configure_logging(settings.log_levels)
    tracer_provider = configure_tracing("agentplane-app")
    try:
        asyncio.run(async_main(settings))
    finally:
        if tracer_provider is not None:
            tracer_provider.shutdown()


async def async_main(settings: Settings) -> None:
    oidc = OIDCSettings()
    async with (
        AsyncExitStack() as upstream_clients,
        ApiClient(configuration=await kubernetes_configuration(settings.kubeconfig)) as api,
        httpx.AsyncClient(base_url=str(settings.action_federation.service_url), timeout=10) as mcp_callback_http,
        dial(
            f"{settings.action_federation.service_url.host}:{settings.action_federation.service_url.port}"
        ) as actions_health_channel,
        dial(f"{settings.egress_admin_url.host}:{settings.egress_admin_url.port}") as egress_channel,
        dial(f"{settings.notifications_url.host}:{settings.notifications_url.port}") as notifications_channel,
        dial(settings.runner_gateway_target) as gateway_channel,
        dial(settings.session_dispatcher_target) as dispatcher_channel,
        dial(settings.history_service_target) as history_health_channel,
        dial(
            f"{settings.connection_service_url.host}:{settings.connection_service_url.port}"
        ) as connection_health_channel,
        HistoryServiceClient(
            settings.history_service_target,
            token_file=settings.history_service_token_file,
            request_timeout_s=settings.history_service_request_timeout_s,
        ) as history,
        history.new_client() as history_watch_client,
        history.new_client() as session_openings_client,
    ):
        # Cast so `patch_namespaced_custom_object` accepts `_content_type` (see util.kubernetes).
        custom_objects = cast(CustomObjectsClient, CustomObjectsApi(api))
        core_v1 = CoreV1Api(api)
        inventory = SandboxAccess(
            SandboxInventory(namespace=settings.sandbox_namespace, custom_objects=custom_objects, core_v1=core_v1),
            default_policy_sets=settings.default_policy_sets,
            caller=ServiceAccountRef(namespace=settings.sandbox_namespace, name="app"),
        )
        runner_control = SandboxControlClient(
            control_target=settings.runner_gateway_control_target,
            token_file=settings.runner_gateway_token_file,
            request_timeout_s=settings.runner_gateway_request_timeout_s,
            lifecycle_timeout_s=settings.runner_gateway_lifecycle_timeout_s,
        )
        dispatcher = SessionDispatcherClient(
            dispatcher_channel,
            token_file=settings.session_dispatcher_token_file,
            request_timeout_s=settings.session_dispatcher_request_timeout_s,
        )
        # In the Sandbox's namespace, not the app's: that is where the Action Service matches a
        # binding to the authenticated Sandbox, and where the owner reference cascades.
        action_policy = ActionPolicyInventory(namespace=settings.sandbox_namespace, custom_objects=custom_objects)
        egress = EgressAccess(
            namespace=settings.sandbox_namespace, custom_objects=custom_objects, action_policy=action_policy
        )
        live = LiveIndex(
            freshness=Freshness(stale_after_seconds=settings.resync_seconds * STALE_AFTER_CYCLES), core_v1=core_v1
        )
        watch = watch_for(
            live,
            custom_objects=custom_objects,
            core_v1=core_v1,
            sandbox_namespace=settings.sandbox_namespace,
            resync_seconds=settings.resync_seconds,
        )
        engine = connect(settings.database_url)
        database_updates = create_database_updates(engine.url)
        summaries = Summaries(history)
        content = ContentStore(engine, history_reader=history)
        history_watch = HistoryWatch(
            history_watch_client, database_updates.changes[Channel.THREADS].notify, retry_interval_s=1
        )
        store = ThreadStore(engine, summaries)
        event_logs = EventLogStore(engine, history_reader=history, summaries=summaries)
        runners = RunnerDirectory(live, runner_control)
        session_openings = SessionOpenings(session_openings_client, event_logs, retry_interval_s=1)
        bridge = RunnerBridge(runners=runners, event_logs=event_logs, content=content, dispatcher=dispatcher)
        decisions = DecisionsClient(
            egress_channel, token_file=settings.egress_admin_token_file, request_timeout_s=settings.egress_admin_timeout
        )
        system_probes = [
            grpc_health_probe("Runner Gateway", gateway_channel),
            grpc_health_probe("History Service", history_health_channel),
            grpc_health_probe("Action Service", actions_health_channel),
            grpc_health_probe("Connection Service", connection_health_channel),
            grpc_health_probe("Notification Service", notifications_channel),
            grpc_health_probe("Egress proxy", egress_channel),
        ]
        electric = None
        if settings.electric_url is not None:
            electric_http = await upstream_clients.enter_async_context(
                httpx.AsyncClient(base_url=str(settings.electric_url), timeout=httpx.Timeout(65, connect=5))
            )
            electric = ElectricProxy(
                electric_http, content, event_logs=event_logs, thread_changes=database_updates.changes[Channel.THREADS]
            )
            system_probes.append(http_health_probe("Electric", electric_http, "/v1/health"))
        app = create_app(
            inventory,
            bridge,
            store,
            settings.models,
            egress,
            decisions,
            live,
            action_policy,
            oidc,
            TokenReviewer(AuthenticationV1Api(api), audience=settings.token_audience, subjects=settings.token_subjects),
            operator_actions=FederatedOperatorActions(settings.action_federation, oidc),
            electric=electric,
            presets=PresetCatalog(sandboxes=settings.sandbox_presets, threads=settings.thread_presets),
            new_sandbox_defaults=settings.new_sandbox_defaults,
            frontend_config=settings.frontend_config,
            event_logs=event_logs,
            content=content,
            database_updates=database_updates,
            operator_sessions=OperatorSessionStore(engine),
            notifications=NotificationsOperatorClient(
                notifications_channel,
                token_file=settings.notifications_token_file,
                request_timeout_s=settings.notifications_request_timeout_s,
            ),
            action_service=action_service_pb2_grpc.ActionServiceStub(actions_health_channel),
            operator_action_service=operator_action_service_pb2_grpc.OperatorActionServiceStub(actions_health_channel),
            web_push_service=web_push_service_pb2_grpc.WebPushServiceStub(actions_health_channel),
            operator_grpc=OperatorGrpcClient(request_timeout_s=10),
            mcp_callback_http=mcp_callback_http,
            connection_service=connection_service_pb2_grpc.ConnectionServiceStub(connection_health_channel),
            system_status=SystemStatusChecker(system_probes, timeout_s=SYSTEM_STATUS_TIMEOUT_S),
        )
        async with AsyncExitStack() as tasks:
            await tasks.enter_async_context(background(watch.run(), name="live-watch"))
            await tasks.enter_async_context(background(history_watch.run(), name="history-watch"))
            await serve_then_close(
                AppServer(
                    uvicorn.Config(
                        app,
                        host=settings.host,
                        port=settings.port,
                        access_log=False,
                        timeout_graceful_shutdown=settings.shutdown_timeout,
                    ),
                    drain_of(app),
                ),
                session_openings=session_openings,
                database_updates=database_updates,
                engine=engine,
            )


async def serve_then_close(
    server: uvicorn.Server,
    *,
    session_openings: SessionOpenings,
    database_updates: WakeupListener,
    engine: AsyncEngine,
    sockets: list[socket.socket] | None = None,
) -> None:
    """Serve until told to exit, then let go in the order the budgets assume: Uvicorn's graceful-shutdown
    timeout bounds the requests and streams still open, and stopping Session openings and closing the
    runner connections and the database have the rest of the Pod's grace period to themselves.

    ``sockets`` lets lifecycle tests hand over a listener they own already."""
    async with AsyncExitStack() as stack:
        stack.push_async_callback(engine.dispose)
        await stack.enter_async_context(database_updates.listener.listen())

        async def run_session_openings() -> None:
            try:
                await session_openings.run()
                raise RuntimeError("Session openings worker stopped unexpectedly")
            finally:
                server.should_exit = True

        async with anyio.create_task_group() as tasks:
            tasks.start_soon(run_session_openings, name="session-openings")
            try:
                # A worker failure requests Uvicorn shutdown above. Let its drain
                # finish before propagating the task-group error out of the process.
                with anyio.CancelScope(shield=True):
                    await server.serve(sockets)
            finally:
                tasks.cancel_scope.cancel()


if __name__ == "__main__":
    main()
