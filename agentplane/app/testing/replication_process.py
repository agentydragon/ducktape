"""Real app processes with a test-only gate at browser sync delivery.

Process readiness and replay observations travel through a pipe, never filesystem sentinels.
The replay gate holds actual Electric/reconciliation response bytes without rewriting data.
"""

import asyncio
import logging
import multiprocessing
import signal
import socket
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from multiprocessing.connection import Connection
from multiprocessing.process import BaseProcess
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import cast
from uuid import UUID, uuid4

import grpc
import httpx
import uvicorn
from kubernetes_asyncio import client as k8s_client
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from agentplane.app.action_policy import ActionPolicyInventory
from agentplane.app.api import create_app
from agentplane.app.database import connect
from agentplane.app.database_updates import Channel, create_database_updates
from agentplane.app.egress_access import EgressAccess
from agentplane.app.electric import ElectricProxy
from agentplane.app.identity import CallerIdentity, CallerKind, require_caller
from agentplane.app.live import LiveIndex
from agentplane.app.model_catalog import ModelCatalog, ModelOption
from agentplane.app.notifications_operator_client import NotificationsOperatorClient
from agentplane.app.operator_sessions import OperatorSessionStore
from agentplane.app.sandbox_access import SandboxAccess
from agentplane.app.testing.model_test_data import TEST_REASONING_EFFORTS
from agentplane.app.testing.notifications_operator import FakeNotificationsOperator
from agentplane.app.testing.operator_streams import OIDC, with_test_login
from agentplane.app.testing.system_status import answering_system_status
from agentplane.app.threads.bridge import RunnerBridge
from agentplane.app.threads.dispatcher_client import SessionDispatcherClient
from agentplane.app.threads.events.event_log import EventLogStore
from agentplane.app.threads.history_watch import HistoryWatch, SessionOpenings
from agentplane.app.threads.runner_directory import RunnerDirectory
from agentplane.app.threads.store import ThreadStore
from agentplane.app.threads.summaries import Summaries
from agentplane.app.threads.view.content import ContentStore
from agentplane.egress.decisions_client import DecisionsClient
from agentplane.grpc.calls import dial
from agentplane.grpc.serving import serving
from agentplane.history_service.client import HistoryServiceClient
from agentplane.kubernetes_watch import Freshness
from agentplane.notification_service import notifications_operator_pb2_grpc
from agentplane.runner.harness import Harness
from agentplane.runner_gateway.client import SandboxControlClient
from agentplane.sandbox_resource import SANDBOXES_PLURAL
from agentplane.sandboxes.inventory import SandboxInventory
from agentplane.sandboxes.testing.sandbox_namespace import SandboxNamespace
from agentplane.testing.kubernetes_apiserver import kubernetes_api
from util.kubernetes import CustomObjectsClient
from util.testing.undeclared_outputs import undeclared_outputs_dir

# gazelle:include_dep @pypi//protobuf
# gazelle:include_dep @pypi//asyncpg


@dataclass(frozen=True)
class Ready:
    url: str


@dataclass(frozen=True)
class SandboxesAccess:
    """The API server an app process reads, and the namespace of the Sandboxes it serves."""

    kubeconfig: Path
    namespace: str


@dataclass(frozen=True)
class GatewayAccess:
    """The Runner Gateway an app process reaches."""

    target: str
    # The app's projected token, for the Runner proxy and the Dispatcher gRPC API.
    token_file: Path


@dataclass(frozen=True)
class ReplayHeld:
    cursor: int


class ReplayAction(StrEnum):
    RELEASE = "release"
    DISCONNECT = "disconnect"


class ReplayGate:
    def __init__(self, after_cursor: int, connection: Connection) -> None:
        self.after_cursor = after_cursor
        self._connection = connection
        self._release: asyncio.Task[ReplayAction] | None = None

    async def hold(self, cursor: int) -> ReplayAction:
        if self._release is None:
            self._release = asyncio.create_task(self._wait_for_release())
        if not self._release.done():
            self._connection.send(ReplayHeld(cursor))
        # Closing the old browser document cancels its response, not the gate shared with the
        # reloaded document's new sync connection.
        pending = self._release
        action = await asyncio.shield(pending)
        if action is ReplayAction.DISCONNECT and self._release is pending:
            self._release = None
        return action

    async def _wait_for_release(self) -> ReplayAction:
        command = await receive(self._connection)
        assert isinstance(command, ReplayAction), command
        return command


class GatedConversationDelivery:
    def __init__(self, app: ASGIApp, *, gate: ReplayGate, event_logs: EventLogStore) -> None:
        self._app = app
        self._gate = gate
        self._event_logs = event_logs

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = scope.get("path", "")
        selected = scope["type"] == "http" and path.endswith("/sync/entities")
        if not selected:
            await self._app(scope, receive, send)
            return
        thread_id = UUID(path.split("/")[2])
        messages: list[Message] = []
        streaming = started = interrupted = False

        async def gated_send(message: Message) -> None:
            nonlocal streaming, started, interrupted
            if interrupted:
                return
            messages.append(message)
            if message["type"] == "http.response.start":
                streaming = dict(message["headers"]).get(b"content-type", b"").startswith(b"text/event-stream")
            # A finite, bounded-interest response is held whole. A live shape's SSE response does
            # not finish, so each of its chunks is held as it comes. The real backend continues
            # ingesting and Electric still owns snapshot/offset reconciliation.
            if message["type"] != "http.response.body" or (message.get("more_body", False) and not streaming):
                return
            cursor = await self._event_logs.last_cursor(thread_id)
            if cursor > self._gate.after_cursor and await self._gate.hold(cursor) is ReplayAction.DISCONNECT:
                interrupted = True
                if started:
                    # An SSE response under way ends; the client reconnects from its own offset.
                    await send({"type": "http.response.body", "body": b"", "more_body": False})
                    return
                await send({"type": "http.response.start", "status": 503, "headers": []})
                await send({"type": "http.response.body", "body": b"test transport interruption"})
                return
            started = True
            for buffered in messages:
                await send(buffered)
            messages.clear()

        await self._app(scope, receive, gated_send)


class EmptyActionsWatch:
    """The browser shell's Actions badge has no configured Action Service in this fixture."""

    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] != "http"
            or scope["method"] != "GET"
            or scope["path"] != "/upstream/actions/v1/action-requests:watch"
        ):
            await self._app(scope, receive, send)
            return

        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"application/x-ndjson"), (b"cache-control", b"no-cache")],
            }
        )
        # The app shell subscribes on every route. Keep this unrelated watch healthy and empty instead
        # of letting the intentionally unconfigured Action Service retry and draw a status spinner.
        await send(
            {
                "type": "http.response.body",
                "body": (b'{"result":{"snapshot":{"actionRequests":[]}}}\n{"result":{"snapshotComplete":{}}}\n'),
                "more_body": True,
            }
        )
        while True:
            if (await receive())["type"] == "http.disconnect":
                return


class ReadyServer(uvicorn.Server):
    def __init__(self, config: uvicorn.Config, connection: Connection, url: str) -> None:
        super().__init__(config)
        self._connection = connection
        self._url = url

    async def startup(self, sockets: list[socket.socket] | None = None) -> None:
        await super().startup(sockets)
        assert self.started
        self._connection.send(Ready(self._url))


def _run(
    database_url: str,
    sandboxes: SandboxesAccess,
    gateway: GatewayAccess | None,
    history_target: str,
    history_token_file: Path,
    connection: Connection,
    browser_login: bool,
    replay_after: int | None,
    electric_url: str | None,
    electric_log_path: Path | None,
) -> None:
    electric_logger = logging.getLogger("agentplane.app.electric")
    previous_level = electric_logger.level
    previous_propagate = electric_logger.propagate
    handler = None
    if electric_log_path is not None:
        # Preserve ElectricProxy's existing upstream timings without enabling app-wide INFO logs.
        handler = logging.FileHandler(electric_log_path, encoding="utf-8")
        handler.setLevel(logging.INFO)
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
        electric_logger.addHandler(handler)
        electric_logger.setLevel(logging.INFO)
        electric_logger.propagate = False
    try:
        asyncio.run(
            _serve(
                database_url,
                sandboxes,
                gateway,
                history_target,
                history_token_file,
                connection,
                browser_login,
                replay_after,
                electric_url,
            )
        )
    finally:
        if handler is not None:
            electric_logger.removeHandler(handler)
            handler.close()
            electric_logger.setLevel(previous_level)
            electric_logger.propagate = previous_propagate


async def _serve(
    database_url: str,
    sandboxes: SandboxesAccess,
    gateway: GatewayAccess | None,
    history_target: str,
    history_token_file: Path,
    connection: Connection,
    browser_login: bool,
    replay_after: int | None,
    electric_url: str | None,
) -> None:
    engine = connect(database_url)
    database_updates = create_database_updates(engine.url)
    with TemporaryDirectory() as directory:
        if gateway is None:
            token_file = Path(directory) / "token"
            await asyncio.to_thread(token_file.write_text, "test-app-unused-token")
            target = "test-unused-runner-gateway.invalid:8080"
        else:
            token_file, target = gateway.token_file, gateway.target
        history = HistoryServiceClient(history_target, token_file=history_token_file, request_timeout_s=10)
        summaries = Summaries(history)
        event_logs = EventLogStore(engine, history_reader=history, summaries=summaries)
        content = ContentStore(engine, history_reader=history)
        notices = grpc.aio.server()
        # Open and silent, as an idle inbox's notices are.
        notifications_operator_pb2_grpc.add_NotificationsOperatorServicer_to_server(
            FakeNotificationsOperator(), notices
        )
        async with (
            kubernetes_api(sandboxes.kubeconfig) as kubernetes,
            history,
            history.new_client() as watch_client,
            history.new_client() as openings_client,
            dial("test-unused-decisions.invalid:8081") as decisions_channel,
            serving(notices, "127.0.0.1:0", grace_s=0) as notices_port,
            dial(f"127.0.0.1:{notices_port}") as notifications_channel,
            httpx.AsyncClient(
                base_url=electric_url or "http://test-unused-electric.invalid", timeout=65
            ) as electric_http,
            dial(target) as dispatcher_channel,
        ):
            namespace = SandboxNamespace(
                name=sandboxes.namespace,
                custom_objects=cast(CustomObjectsClient, k8s_client.CustomObjectsApi(kubernetes.api)),
                core_v1=k8s_client.CoreV1Api(kubernetes.api),
                kubeconfig=sandboxes.kubeconfig,
            )
            custom, core = namespace.custom_objects, namespace.core_v1
            # The Sandboxes and Pods the namespace holds, as the app's watch would have listed them.
            index = LiveIndex(
                freshness=Freshness(
                    stale_after_seconds=90, at={"sandboxes": datetime.now(UTC), "pods": datetime.now(UTC)}
                ),
                core_v1=core,
            )
            index.sandboxes.update(
                {item["metadata"]["name"]: item for item in await namespace.objects(SANDBOXES_PLURAL)}
            )
            index.pods.update({pod.metadata.name: pod for pod in await namespace.pods()})
            inventory = SandboxAccess(
                SandboxInventory(namespace=namespace.name, custom_objects=custom, core_v1=core),
                default_policy_sets=frozenset(),
                caller=namespace.account("app"),
            )
            control = SandboxControlClient(
                control_target=target, token_file=token_file, request_timeout_s=10, lifecycle_timeout_s=10
            )
            store = ThreadStore(engine, summaries)
            runners = RunnerDirectory(index, control)
            dispatcher = SessionDispatcherClient(dispatcher_channel, token_file=token_file, request_timeout_s=10)
            bridge = RunnerBridge(runners=runners, event_logs=event_logs, content=content, dispatcher=dispatcher)
            history_watch = HistoryWatch(
                watch_client, database_updates.changes[Channel.THREADS].notify, retry_interval_s=0.05
            )
            action_policy = ActionPolicyInventory(namespace=namespace.name, custom_objects=custom)
            egress = EgressAccess(namespace=namespace.name, custom_objects=custom, action_policy=action_policy)
            app = create_app(
                inventory,
                bridge,
                store,
                ModelCatalog(
                    models=[
                        ModelOption(
                            model="test-model-before",
                            display_name="Test Model Before",
                            reasoning_efforts=list(TEST_REASONING_EFFORTS),
                        ),
                        ModelOption(
                            model="test-model-after",
                            display_name="Test Model After",
                            reasoning_efforts=list(TEST_REASONING_EFFORTS),
                        ),
                    ],
                    harnesses={harness: ["test-model-before", "test-model-after"] for harness in Harness},
                ),
                egress,
                DecisionsClient(decisions_channel, token_file=Path(directory) / "egress-token", request_timeout_s=1),
                index,
                action_policy,
                # A browser's app has operator sessions, which the operator-only relays require.
                OIDC if browser_login else None,
                electric=(
                    ElectricProxy(
                        electric_http,
                        content,
                        event_logs=event_logs,
                        thread_changes=database_updates.changes[Channel.THREADS],
                    )
                    if electric_url is not None
                    else None
                ),
                event_logs=event_logs,
                content=content,
                database_updates=database_updates,
                operator_sessions=OperatorSessionStore(engine),
                notifications=NotificationsOperatorClient(
                    notifications_channel, token_file=Path(directory) / "notifications-token", request_timeout_s=1
                ),
                system_status=answering_system_status(),
            )
            # Authentication is tested separately; the production routes, HTTP transport, ingestion,
            # PostgreSQL notifications, and SSE generator all run here unchanged.
            app.dependency_overrides[require_caller] = lambda: CallerIdentity(CallerKind.OPERATOR, "test-operator")
            if browser_login:
                with_test_login(app)
                app.add_middleware(EmptyActionsWatch)
            if replay_after is not None:
                app.add_middleware(
                    GatedConversationDelivery, gate=ReplayGate(replay_after, connection), event_logs=event_logs
                )
            try:
                async with database_updates.listener.listen():
                    watching = asyncio.create_task(history_watch.run(), name="test-app-history-watch")
                    opening = asyncio.create_task(
                        SessionOpenings(openings_client, event_logs, retry_interval_s=0.05).run(),
                        name="test-app-session-openings",
                    )
                    try:
                        with socket.socket() as listener:
                            listener.bind(("127.0.0.1", 0))
                            url = f"http://127.0.0.1:{listener.getsockname()[1]}"
                            await ReadyServer(uvicorn.Config(app, log_level="warning"), connection, url).serve(
                                sockets=[listener]
                            )
                    finally:
                        watching.cancel()
                        opening.cancel()
                        await asyncio.gather(watching, opening, return_exceptions=True)
            finally:
                await engine.dispose()


async def receive(connection: Connection) -> object:
    ready = asyncio.Event()
    loop = asyncio.get_running_loop()
    loop.add_reader(connection.fileno(), ready.set)
    try:
        await ready.wait()
        return connection.recv()
    finally:
        loop.remove_reader(connection.fileno())


@dataclass
class AppProcess:
    url: str
    process: BaseProcess
    observations: Connection

    async def replay_held(self) -> ReplayHeld:
        observation = await receive(self.observations)
        assert isinstance(observation, ReplayHeld), observation
        return observation

    def release_replay(self) -> None:
        self.observations.send(ReplayAction.RELEASE)

    def disconnect_replay(self) -> None:
        self.observations.send(ReplayAction.DISCONNECT)

    async def kill(self) -> None:
        self.process.kill()
        await asyncio.to_thread(self.process.join)
        assert self.process.exitcode == -signal.SIGKILL


@asynccontextmanager
async def app_process(
    database_url: str,
    history: HistoryServiceClient,
    *,
    sandboxes: SandboxesAccess,
    gateway: GatewayAccess | None,
    browser_login: bool = False,
    replay_after: int | None = None,
    electric_url: str | None = None,
) -> AsyncIterator[AppProcess]:
    context = multiprocessing.get_context("spawn")
    parent, child = context.Pipe()
    electric_log_path = (
        undeclared_outputs_dir() / f"electric-proxy-{uuid4().hex}.log"
        if browser_login and electric_url is not None
        else None
    )
    process = context.Process(
        target=_run,
        args=(
            database_url,
            sandboxes,
            gateway,
            history.target,
            history.token_file,
            child,
            browser_login,
            replay_after,
            electric_url,
            electric_log_path,
        ),
    )
    process.start()
    child.close()
    try:
        ready = await receive(parent)
        assert isinstance(ready, Ready), ready
        yield AppProcess(ready.url, process, parent)
    finally:
        if process.is_alive():
            process.kill()
        await asyncio.to_thread(process.join)
        parent.close()
        process.close()
