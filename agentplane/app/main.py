"""Serve the Agentplane app over one namespace's sandbox inventory."""

from __future__ import annotations

import asyncio
import logging
import mimetypes
import os
import socket
from collections.abc import MutableMapping
from pathlib import Path
from typing import Any, cast

import httpx
import uvicorn
from fastapi import Response
from fastapi.staticfiles import StaticFiles
from kubernetes_asyncio import client as k8s_client, config as k8s_config
from kubernetes_asyncio.client import ApiClient, AuthenticationV1Api, CoreV1Api, CustomObjectsApi
from sqlalchemy.ext.asyncio import AsyncEngine

from agentplane.app.action_federation import FederatedOperatorActions
from agentplane.app.action_policy import ActionPolicyInventory
from agentplane.app.api import create_app
from agentplane.app.database import connect
from agentplane.app.database_updates import Channel, DatabaseUpdates
from agentplane.app.decisions import DecisionsClient
from agentplane.app.egress_access import EgressAccess
from agentplane.app.electric import ElectricProxy
from agentplane.app.identity import TokenReviewer
from agentplane.app.live import LiveIndex, watch_for
from agentplane.app.oidc import load_settings
from agentplane.app.operator_sessions import OperatorSessionStore
from agentplane.app.presets import PresetCatalog
from agentplane.app.settings import Settings
from agentplane.app.shutdown import Drain, drain_of
from agentplane.app.threads.bridge import RunnerBridge
from agentplane.app.threads.events.event_log import EventLogStore
from agentplane.app.threads.ingestion import Ingester, Ingestion
from agentplane.app.threads.sessions import SandboxSessions
from agentplane.app.threads.store import ThreadStore
from agentplane.app.threads.view.content import ContentStore
from agentplane.kubernetes_watch import STALE_AFTER_CYCLES
from agentplane.sandbox_service.client import SandboxServiceClient
from agentplane.sandbox_service.egress_views import EgressReader
from util.bazel.runfiles import get_required_path
from util.kubernetes import CustomObjectsClient

# The built frontend, a runfiles data dependency of this module's library.
# The bundle's entry; runfiles resolve files, not directories, so the mount is its parent.
FRONTEND_INDEX = "_main/agentplane/app/frontend/dist/index.html"
SERVICE_WORKER = "_main/agentplane/app/frontend/sw.js"


logger = logging.getLogger(__name__)


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
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    asyncio.run(async_main(Settings()))


async def async_main(settings: Settings) -> None:
    oidc = load_settings()
    if settings.action_federation is not None and oidc is None:
        raise ValueError("Action federation requires operator OIDC login")
    configuration = k8s_client.Configuration()
    if settings.kubeconfig is None:
        k8s_config.load_incluster_config(client_configuration=configuration)
    else:
        await k8s_config.load_kube_config(config_file=str(settings.kubeconfig), client_configuration=configuration)
    async with (
        ApiClient(configuration=configuration) as api,
        httpx.AsyncClient(
            base_url=str(settings.action_federation.service_url)
            if settings.action_federation
            else "http://disabled.invalid",
            timeout=10,
        ) as actions_http,
        httpx.AsyncClient(
            base_url=settings.notifications_url or "http://disabled.invalid", timeout=5
        ) as notifications_http,
        httpx.AsyncClient(base_url=settings.egress_admin_url, timeout=settings.egress_admin_timeout) as admin_http,
        httpx.AsyncClient(
            base_url=settings.electric_url or "http://disabled.invalid", timeout=httpx.Timeout(65, connect=5)
        ) as electric_http,
    ):
        # Cast so `patch_namespaced_custom_object` accepts `_content_type` (see util.kubernetes).
        custom_objects = cast(CustomObjectsClient, CustomObjectsApi(api))
        inventory = SandboxServiceClient(
            settings.sandbox_service_target,
            namespace=settings.sandbox_namespace,
            token_file=settings.sandbox_service_token_file,
            command_admission_timeout_s=settings.sandbox_service_command_admission_timeout_s,
            request_timeout_s=settings.sandbox_service_request_timeout_s,
            lifecycle_timeout_s=settings.sandbox_service_lifecycle_timeout_s,
            follow_timeout_s=settings.sandbox_service_follow_timeout_s,
            channel_options=settings.sandbox_service_grpc_channel_options,
        )
        egress = EgressAccess(EgressReader(namespace=settings.namespace, custom_objects=custom_objects), inventory)
        # In the Sandbox's namespace, not the app's: that is where the Action Service matches a
        # binding to the authenticated Sandbox, and where the owner reference cascades.
        action_policy = ActionPolicyInventory(namespace=settings.sandbox_namespace, custom_objects=custom_objects)
        core_v1 = CoreV1Api(api)
        live = LiveIndex(stale_after_seconds=float(settings.resync_seconds * STALE_AFTER_CYCLES), core_v1=core_v1)
        watch = watch_for(
            live,
            custom_objects=custom_objects,
            core_v1=core_v1,
            namespace=settings.namespace,
            sandbox_namespace=settings.sandbox_namespace,
            resync_seconds=settings.resync_seconds,
        )
        engine = connect(settings.database_url)
        database_updates = DatabaseUpdates(engine.url)
        store = ThreadStore(engine)
        event_logs = EventLogStore(engine, history_reader=inventory if settings.history_reads_enabled else None)
        content = ContentStore(engine)
        runners = SandboxSessions(live, inventory)
        ingester = Ingester(runners=runners, event_logs=event_logs, ingestion=Ingestion(engine))
        bridge = RunnerBridge(runners=runners, event_logs=event_logs, content=content, ingester=ingester)

        operator_actions = (
            FederatedOperatorActions(settings.action_federation, oidc, actions_http)
            if settings.action_federation is not None and oidc is not None
            else None
        )
        logger.info("browser login: %s", f"OIDC at {oidc.issuer}" if oidc else "none configured")
        app = create_app(
            inventory,
            bridge,
            store,
            settings.models,
            egress,
            DecisionsClient(admin_http),
            live,
            action_policy,
            oidc,
            TokenReviewer(AuthenticationV1Api(api), audience=settings.token_audience, subjects=settings.token_subjects),
            operator_actions=operator_actions,
            electric=(
                ElectricProxy(
                    electric_http,
                    content,
                    event_logs=event_logs,
                    thread_changes=database_updates.changes[Channel.THREADS],
                )
                if settings.electric_url is not None
                else None
            ),
            presets=PresetCatalog(sandboxes=settings.sandbox_presets, threads=settings.thread_presets),
            kubernetes_grants=settings.kubernetes_grants,
            event_logs=event_logs,
            content=content,
            database_updates=database_updates,
            operator_sessions=OperatorSessionStore(engine),
            notifications_http=notifications_http if settings.notifications_url else None,
            notifications_token_file=settings.notifications_token_file,
        )
        worker = await asyncio.to_thread(Path(get_required_path(SERVICE_WORKER)).read_bytes)

        @app.get("/sw.js")
        async def service_worker() -> Response:
            return Response(content=worker, media_type="application/javascript", headers={"Cache-Control": "no-store"})

        # The SPA, mounted last so the API routes above it win; index.html answers the rest.
        app.mount("/", SpaFiles(directory=get_required_path(FRONTEND_INDEX).parent, html=True), name="frontend")
        watch_task = asyncio.create_task(watch.run(), name="live-watch")
        try:
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
                ingester=ingester,
                runners=runners,
                database_updates=database_updates,
                engine=engine,
            )
        finally:
            watch_task.cancel()
            await asyncio.gather(watch_task, return_exceptions=True)


async def serve_then_close(
    server: uvicorn.Server,
    *,
    ingester: Ingester,
    runners: SandboxSessions,
    database_updates: DatabaseUpdates,
    engine: AsyncEngine,
) -> None:
    """Serve until told to exit, then let go in the order the budgets assume: Uvicorn's graceful-shutdown
    timeout bounds the requests and streams still open, and the ingester's lease release and closing the
    runner connections and the database have the rest of the Pod's grace period to themselves."""
    try:
        async with database_updates.listener.listen():
            try:
                await ingester.start()
                await server.serve()
            finally:
                await ingester.close()
                await runners.close()
    finally:
        await engine.dispose()


if __name__ == "__main__":
    main()
