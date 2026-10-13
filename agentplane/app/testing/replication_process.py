"""Real app processes with a test-only gate at browser sync delivery.

Process readiness and replay observations travel through a pipe, never filesystem sentinels.
The replay gate holds actual Electric/reconciliation response bytes without rewriting data.
"""

import asyncio
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
from typing import Any, cast
from uuid import UUID

import httpx
import uvicorn
from fastapi.staticfiles import StaticFiles
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
from agentplane.app.operator_sessions import OperatorSessionStore
from agentplane.app.testing.model_test_data import TEST_REASONING_EFFORTS
from agentplane.app.testing.replication_source import SANDBOX
from agentplane.app.testing.system_status import answering_system_status
from agentplane.app.threads.bridge import RunnerBridge
from agentplane.app.threads.events.event_log import EventLogStore
from agentplane.app.threads.history_watch import HistoryWatch, SessionOpenings
from agentplane.app.threads.runner_directory import RunnerDirectory
from agentplane.app.threads.store import ThreadStore
from agentplane.app.threads.summaries import Summaries
from agentplane.app.threads.view.content import ContentStore
from agentplane.egress.decisions_client import DecisionsClient
from agentplane.grpc_calls import dial
from agentplane.history_service.client import HistoryServiceClient
from agentplane.kubernetes_watch import Freshness
from agentplane.runner.harness import Harness
from agentplane.runner_gateway.client import RunnerGatewayClient, SandboxControlClient
from agentplane.sandbox_service.client import SandboxServiceClient
from agentplane.sandbox_service.testing.backend import seed_runner
from agentplane.sandbox_service.testing.fake_inventory import NAMESPACE, FakeCoreV1Api, FakeCustomObjectsApi

# gazelle:include_dep @pypi//protobuf
# gazelle:include_dep @pypi//asyncpg


@dataclass(frozen=True)
class Ready:
    url: str


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
    service_port: int,
    history_target: str,
    history_token_file: Path,
    connection: Connection,
    frontend_directory: Path | None,
    sandbox_present: bool,
    replay_after: int | None,
    electric_url: str | None,
) -> None:
    asyncio.run(
        _serve(
            database_url,
            service_port,
            history_target,
            history_token_file,
            connection,
            frontend_directory,
            sandbox_present,
            replay_after,
            electric_url,
        )
    )


async def _serve(
    database_url: str,
    service_port: int,
    history_target: str,
    history_token_file: Path,
    connection: Connection,
    frontend_directory: Path | None,
    sandbox_present: bool,
    replay_after: int | None,
    electric_url: str | None,
) -> None:
    engine = connect(database_url)
    database_updates = create_database_updates(engine.url)
    custom, core = cast(Any, FakeCustomObjectsApi()), cast(Any, FakeCoreV1Api())
    index = LiveIndex(
        freshness=Freshness(stale_after_seconds=90, at={"sandboxes": datetime.now(UTC), "pods": datetime.now(UTC)}),
        core_v1=core,
    )
    if sandbox_present:
        raw, running = seed_runner(custom, core, SANDBOX)
        index.sandboxes[SANDBOX] = raw
        index.pods[SANDBOX] = running
    with TemporaryDirectory() as directory:
        token_file = Path(directory) / "token"
        await asyncio.to_thread(token_file.write_text, "test-app-sandbox-peer")
        history = HistoryServiceClient(history_target, token_file=history_token_file, request_timeout_s=10)
        summaries = Summaries(history)
        event_logs = EventLogStore(engine, history_reader=history, summaries=summaries)
        content = ContentStore(engine, history_reader=history)
        async with (
            # One peer serves both services.
            dial(f"127.0.0.1:{service_port}") as channel,
            history,
            history.new_client() as watch_client,
            history.new_client() as openings_client,
            httpx.AsyncClient(base_url="http://test-unused-decisions.invalid") as decisions_http,
            httpx.AsyncClient(
                base_url=electric_url or "http://test-unused-electric.invalid", timeout=65
            ) as electric_http,
            httpx.AsyncClient(base_url="http://test-unused-notifications.invalid") as notifications_http,
        ):
            inventory = SandboxServiceClient(
                channel, namespace=NAMESPACE, token_file=token_file, request_timeout_s=10, lifecycle_timeout_s=10
            )
            gateway = RunnerGatewayClient(
                channel,
                token_file=token_file,
                command_admission_timeout_s=10,
                request_timeout_s=10,
                control=SandboxControlClient(
                    control_target=f"127.0.0.1:{service_port}",
                    token_file=token_file,
                    request_timeout_s=10,
                    lifecycle_timeout_s=10,
                ),
            )
            store = ThreadStore(engine, summaries, gateway=gateway)
            bridge = RunnerBridge(runners=RunnerDirectory(index, gateway), event_logs=event_logs, content=content)
            history_watch = HistoryWatch(
                watch_client, database_updates.changes[Channel.THREADS].notify, retry_interval_s=0.05
            )
            action_policy = ActionPolicyInventory(namespace=NAMESPACE, custom_objects=custom)
            egress = EgressAccess(namespace=NAMESPACE, custom_objects=custom, action_policy=action_policy)
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
                DecisionsClient(decisions_http),
                index,
                action_policy,
                electric=ElectricProxy(
                    electric_http,
                    content,
                    event_logs=event_logs,
                    thread_changes=database_updates.changes[Channel.THREADS],
                ),
                event_logs=event_logs,
                content=content,
                database_updates=database_updates,
                operator_sessions=OperatorSessionStore(engine),
                notifications_http=notifications_http,
                notifications_token_file=Path(directory) / "notifications-token",
                system_status=answering_system_status(),
            )
            # Authentication is tested separately; the production routes, HTTP transport, ingestion,
            # PostgreSQL notifications, and SSE generator all run here unchanged.
            app.dependency_overrides[require_caller] = lambda: CallerIdentity(CallerKind.OPERATOR, "test-operator")
            if replay_after is not None:
                app.add_middleware(
                    GatedConversationDelivery, gate=ReplayGate(replay_after, connection), event_logs=event_logs
                )
            if frontend_directory is not None:
                app.mount("/", StaticFiles(directory=frontend_directory, html=True), name="test-frontend")
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
                await core.close()


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
    service_port: int,
    history: HistoryServiceClient,
    *,
    frontend_directory: Path | None = None,
    sandbox_present: bool = True,
    replay_after: int | None = None,
    electric_url: str | None = None,
) -> AsyncIterator[AppProcess]:
    context = multiprocessing.get_context("spawn")
    parent, child = context.Pipe()
    process = context.Process(
        target=_run,
        args=(
            database_url,
            service_port,
            history.target,
            history.token_file,
            child,
            frontend_directory,
            sandbox_present,
            replay_after,
            electric_url,
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
