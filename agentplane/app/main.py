"""Serve the Agentplane app over one namespace's sandbox inventory."""

from __future__ import annotations

import asyncio
import mimetypes
import os
import socket
from collections.abc import MutableMapping
from contextlib import AsyncExitStack, nullcontext
from pathlib import Path
from typing import Any, cast

import httpx
import uvicorn
from fastapi import Response
from fastapi.staticfiles import StaticFiles
from kubernetes_asyncio.client import ApiClient, AuthenticationV1Api, CoreV1Api, CustomObjectsApi
from sqlalchemy.ext.asyncio import AsyncEngine

from agentplane.app.action_federation import FederatedOperatorActions
from agentplane.app.action_policy import ActionPolicyInventory
from agentplane.app.api import create_app
from agentplane.app.database import connect
from agentplane.app.database_updates import Channel, create_database_updates
from agentplane.app.egress_access import EgressAccess
from agentplane.app.electric import ElectricProxy
from agentplane.app.identity import TokenReviewer
from agentplane.app.live import LiveIndex, watch_for
from agentplane.app.oidc import OIDCSettings
from agentplane.app.operator_sessions import OperatorSessionStore
from agentplane.app.presets import PresetCatalog
from agentplane.app.sandbox_access import SandboxAccess
from agentplane.app.settings import Settings
from agentplane.app.shutdown import Drain, drain_of
from agentplane.app.system_status import SystemStatusChecker, grpc_health_probe, http_health_probe
from agentplane.app.threads.bridge import RunnerBridge
from agentplane.app.threads.events.event_log import EventLogStore
from agentplane.app.threads.history_watch import HistoryWatch, SessionOpenings
from agentplane.app.threads.runner_directory import RunnerDirectory
from agentplane.app.threads.store import ThreadStore
from agentplane.app.threads.summaries import Summaries
from agentplane.app.threads.view.content import ContentStore
from agentplane.background_task import background
from agentplane.egress.decisions_client import DecisionsClient
from agentplane.grpc_calls import dial
from agentplane.history_service.client import HistoryServiceClient
from agentplane.kubernetes_watch import STALE_AFTER_CYCLES, Freshness
from agentplane.postgres.wakeups import WakeupListener
from agentplane.runner_gateway.client import RunnerGatewayClient, SandboxControlClient
from agentplane.sandbox_service.client import SandboxServiceClient
from agentplane.sandboxes.inventory import SandboxInventory
from agentplane.service_process import configure_logging, kubernetes_configuration
from agentplane.subjects import ServiceAccountRef
from util.bazel.runfiles import get_required_path
from util.kubernetes import CustomObjectsClient

# The built frontend, a runfiles data dependency of this module's library.
# The bundle's entry; runfiles resolve files, not directories, so the mount is its parent.
FRONTEND_INDEX = "_main/agentplane/app/frontend/dist/index.html"
SERVICE_WORKER = "_main/agentplane/app/frontend/service_worker/bundled/sw.js"

# Each System page probe's budget; the probes run together, so the page answers within it.
SYSTEM_STATUS_TIMEOUT_S = 5


class SpaFiles(StaticFiles):
    """The SPA, served so a browser never keeps a deploy-old copy.

    The bundle keeps one name and Bazel stamps every file with the same fixed mtime, so a plain
    `StaticFiles` mount lets the browser's heuristic freshness reuse `main.js` for months and
    answers a same-sized `index.html` with a false 304 from its mtime-and-size ETag.
    """

    def file_response(
        self,
        full_path: str | os.PathLike[str],
        stat_result: os.stat_result,
        scope: MutableMapping[str, Any],
        status_code: int = 200,
    ) -> Response:
        path = Path(full_path)
        return Response(
            content=path.read_bytes(),
            media_type=mimetypes.guess_type(path.name)[0],
            headers={"Cache-Control": "no-store"},
            status_code=status_code,
        )


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
    asyncio.run(async_main(settings))


async def async_main(settings: Settings) -> None:
    oidc = OIDCSettings()
    async with (
        ApiClient(configuration=await kubernetes_configuration(settings.kubeconfig)) as api,
        httpx.AsyncClient(base_url=str(settings.action_federation.service_url), timeout=10) as actions_http,
        httpx.AsyncClient(base_url=str(settings.notifications_url), timeout=5) as notifications_http,
        httpx.AsyncClient(base_url=str(settings.egress_admin_url), timeout=settings.egress_admin_timeout) as admin_http,
        httpx.AsyncClient(base_url=str(settings.electric_url), timeout=httpx.Timeout(65, connect=5)) as electric_http,
        (
            dial(settings.sandbox_service_target)
            if settings.default_policy_sets is None and settings.sandbox_service_target is not None
            else nullcontext()
        ) as sandbox_service_channel,
        dial(settings.runner_gateway_target) as gateway_channel,
        dial(settings.history_service_target) as history_health_channel,
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
        # CLEANUP(added 2026-10-14): Keep only the SandboxAccess branch, and make `default_policy_sets`
        #   required, once the deployed manifests pass it everywhere.
        inventory: SandboxServiceClient | SandboxAccess
        if settings.default_policy_sets is None:
            assert sandbox_service_channel is not None  # Settings validates the legacy backend's target.
            inventory = SandboxServiceClient(
                sandbox_service_channel,
                namespace=settings.sandbox_namespace,
                token_file=settings.sandbox_service_token_file,
                request_timeout_s=settings.sandbox_service_request_timeout_s,
                lifecycle_timeout_s=settings.sandbox_service_lifecycle_timeout_s,
            )
        else:
            inventory = SandboxAccess(
                SandboxInventory(namespace=settings.sandbox_namespace, custom_objects=custom_objects, core_v1=core_v1),
                default_policy_sets=settings.default_policy_sets,
                caller=ServiceAccountRef(namespace=settings.sandbox_namespace, name="agentplane-app"),
            )
        gateway = RunnerGatewayClient(
            gateway_channel,
            token_file=settings.runner_gateway_token_file,
            command_admission_timeout_s=settings.runner_gateway_command_admission_timeout_s,
            request_timeout_s=settings.sandbox_service_request_timeout_s,
            control=SandboxControlClient(
                control_target=settings.runner_gateway_control_target,
                token_file=settings.runner_gateway_token_file,
                request_timeout_s=settings.sandbox_service_request_timeout_s,
                lifecycle_timeout_s=settings.sandbox_service_lifecycle_timeout_s,
            ),
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
        store = ThreadStore(engine, summaries, gateway=gateway)
        event_logs = EventLogStore(engine, history_reader=history, summaries=summaries)
        runners = RunnerDirectory(live, gateway)
        session_openings = SessionOpenings(session_openings_client, event_logs, retry_interval_s=1)
        bridge = RunnerBridge(runners=runners, event_logs=event_logs, content=content)

        decisions = DecisionsClient(admin_http)
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
            operator_actions=FederatedOperatorActions(settings.action_federation, oidc, actions_http),
            electric=ElectricProxy(
                electric_http, content, event_logs=event_logs, thread_changes=database_updates.changes[Channel.THREADS]
            ),
            presets=PresetCatalog(sandboxes=settings.sandbox_presets, threads=settings.thread_presets),
            event_logs=event_logs,
            content=content,
            database_updates=database_updates,
            operator_sessions=OperatorSessionStore(engine),
            notifications_http=notifications_http,
            notifications_token_file=settings.notifications_token_file,
            system_status=SystemStatusChecker(
                [
                    *(
                        [grpc_health_probe("Sandbox Service", sandbox_service_channel)]
                        if sandbox_service_channel is not None
                        else []
                    ),
                    grpc_health_probe("Runner Gateway", gateway_channel),
                    grpc_health_probe("History Service", history_health_channel),
                    http_health_probe("Action Service", actions_http, "/healthz"),
                    http_health_probe("Notification Service", notifications_http, "/healthz"),
                    http_health_probe("Egress proxy", admin_http, "/healthz"),
                    http_health_probe("Electric", electric_http, "/v1/health"),
                ],
                timeout_s=SYSTEM_STATUS_TIMEOUT_S,
            ),
        )
        worker = await asyncio.to_thread(Path(get_required_path(SERVICE_WORKER)).read_bytes)

        @app.get("/sw.js")
        async def service_worker() -> Response:
            return Response(content=worker, media_type="application/javascript", headers={"Cache-Control": "no-store"})

        # The SPA, mounted last so the API routes above it win; index.html answers the rest.
        app.mount("/", SpaFiles(directory=get_required_path(FRONTEND_INDEX).parent, html=True), name="frontend")
        async with (
            background(watch.run(), name="live-watch"),
            background(history_watch.run(), name="history-watch"),
            background(decisions.watch(retry_interval_s=1), name="egress-decisions-watch"),
        ):
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
    server: uvicorn.Server, *, session_openings: SessionOpenings, database_updates: WakeupListener, engine: AsyncEngine
) -> None:
    """Serve until told to exit, then let go in the order the budgets assume: Uvicorn's graceful-shutdown
    timeout bounds the requests and streams still open, and stopping Session openings and closing the
    runner connections and the database have the rest of the Pod's grace period to themselves."""
    async with AsyncExitStack() as stack:
        stack.push_async_callback(engine.dispose)
        await stack.enter_async_context(database_updates.listener.listen())
        await stack.enter_async_context(background(session_openings.run(), name="session-openings"))
        await server.serve()


if __name__ == "__main__":
    main()
