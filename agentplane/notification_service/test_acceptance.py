"""Real Actions → notification inbox → Session Dispatcher, Runner Gateway and History Service → both harnesses, no app."""

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Callable
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import pytest
import pytest_bazel
from google.protobuf.json_format import MessageToDict
from kubernetes_asyncio import client as k8s_client
from sqlalchemy import update
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine

from agentplane.action_service.api import create_app as actions_app
from agentplane.action_service.auth import DisabledOperatorAuthenticator
from agentplane.action_service.catalog import ActionCatalog
from agentplane.action_service.db import make_sessionmaker
from agentplane.action_service.models import DecisionInput, OperatorPrincipal, Verdict
from agentplane.action_service.service import ActionService
from agentplane.action_service.testing.callers import admitted_callers
from agentplane.action_service.testing.fixtures import RecordingExecutor
from agentplane.action_service.testing.stores import action_store
from agentplane.action_service.updates import ActionUpdateBroker
from agentplane.changes import ChangeSignal
from agentplane.grpc_calls import dial
from agentplane.history_service.channels import Channel, create_wakeups
from agentplane.history_service.testing.backend import history_service, ingest
from agentplane.notification_service.api import create_app
from agentplane.notification_service.db import Inbox
from agentplane.notification_service.dispatcher_client import SessionDispatcherClient
from agentplane.notification_service.service import Service
from agentplane.notification_service.settings import NoticeDebounceSettings
from agentplane.notification_service.sources.actions import ActionServiceClient
from agentplane.notification_service.store import Store
from agentplane.proto import command_pb2, event_pb2, runner_pb2, session_dispatcher_pb2
from agentplane.proto.sandboxes_pb2 import SandboxDestination, ServiceAccount
from agentplane.runner.config import RunnerConfig
from agentplane.runner.platform_instructions import render_platform_instructions
from agentplane.runner.runner import Runner
from agentplane.runner.sandbox_launch import SandboxIdentity
from agentplane.runner.testing.scripted_model import ScriptedModel, Text
from agentplane.sandbox_service.testing.connected_runner import authenticated_service
from agentplane.sandboxes.testing.kubernetes import (
    ACCOUNT,
    POD_UID,
    SANDBOX,
    SANDBOX_UID,
    SandboxCluster,
    sandbox_cluster,
)
from agentplane.session_dispatcher import main as dispatcher_main
from agentplane.session_dispatcher.settings import Settings as DispatcherSettings
from agentplane.session_dispatcher.testing.sandbox_status import set_sandbox
from agentplane.subjects import ServiceAccountRef
from agentplane.testing.fake_apiserver import SANDBOX_NAMESPACE, TokenVerdict
from agentplane.workload_auth.principal import WorkloadPrincipalResolver
from util.net import pick_free_port

# gazelle:include_dep @pypi//protobuf


@asynccontextmanager
async def running_dispatcher(
    cluster: SandboxCluster, *, settings: DispatcherSettings, monkeypatch: pytest.MonkeyPatch, caller_token_file: Path
) -> AsyncIterator[SessionDispatcherClient]:
    """Run the production Dispatcher against the fake cluster and connected Gateway fixture."""
    set_sandbox(cluster, ready=True, incarnation=POD_UID, pod_name=SANDBOX)
    synced = asyncio.Event()

    class ObservedSandboxWatch(dispatcher_main.SandboxWatch):
        async def run(self) -> None:
            watcher = asyncio.create_task(super().run())
            try:
                await self.wait_synced()
                synced.set()
                await watcher
            finally:
                watcher.cancel()
                await asyncio.gather(watcher, return_exceptions=True)

    async def configuration(path: Path | None) -> k8s_client.Configuration:
        del path
        return cluster.api.configuration

    stopping = asyncio.Event()

    async def until_stopped() -> None:
        await stopping.wait()

    monkeypatch.setattr(dispatcher_main, "SandboxWatch", ObservedSandboxWatch)
    monkeypatch.setattr(dispatcher_main, "kubernetes_configuration", configuration)
    monkeypatch.setattr(dispatcher_main, "until_terminated", until_stopped)
    task = asyncio.create_task(dispatcher_main.serve(settings), name="notification-acceptance-dispatcher")
    try:
        async with dial(f"{settings.host}:{settings.port}") as channel, asyncio.timeout(5):
            await channel.channel_ready()
            await synced.wait()
            yield SessionDispatcherClient(channel, token_file=caller_token_file, request_timeout_s=10)
    finally:
        stopping.set()
        await task


@pytest.fixture
def config(config: RunnerConfig) -> RunnerConfig:
    """The runner starts every Session with the deployment's real platform instructions."""
    platform_instructions = render_platform_instructions(
        egress_api_url="http://egress.test",
        actions_service_url="http://actions.test",
        notifications_service_url="http://notifications.test",
    )
    return replace(
        config,
        launch=replace(
            config.launch,
            sandbox=SandboxIdentity(namespace=SANDBOX_NAMESPACE, name=SANDBOX, uid=str(SANDBOX_UID)),
            platform_instructions=platform_instructions,
        ),
    )


@pytest.mark.parametrize("busy", [False, True])
@pytest.mark.parametrize("failed_before_rpc", [False, True])
async def test_listen_deliver_read_ack_and_recover_lost_response_without_app(
    store: Store,
    restart: Callable[[Service], Service],
    action_engine: AsyncEngine,
    echo_catalog: ActionCatalog,
    echo_executor: RecordingExecutor,
    runner: Runner,
    spec: runner_pb2.SessionSpec,
    model: ScriptedModel,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    sandbox_service_db_url: str,
    dispatcher_db_url: str,
    busy: bool,
    failed_before_rpc: bool,
) -> None:
    caplog.set_level(logging.INFO, logger=__name__)
    owner = ServiceAccountRef(namespace=SANDBOX_NAMESPACE, name=ACCOUNT)
    delegate = ServiceAccountRef(namespace=SANDBOX_NAMESPACE, name="notifications")
    other = ServiceAccountRef(namespace=SANDBOX_NAMESPACE, name="other")
    action_service = ActionService(
        action_store(make_sessionmaker(action_engine)),
        echo_catalog,
        {"agentplane": echo_executor},
        dispatches=ChangeSignal(),
    )
    async with action_service, sandbox_cluster() as cluster, asyncio.timeout(60), AsyncExitStack() as stack:
        for token, account in [("owner-token", owner), ("delegate-token", delegate), ("other-token", other)]:
            cluster.fake.tokens[token] = TokenVerdict(
                username=f"system:serviceaccount:{account.namespace}:{account.name}",
                pod_name=f"{account.name}-pod",
                pod_uid=f"{account.name}-pod-uid",
                audiences=("test-notifications",),
            )
        resolver = WorkloadPrincipalResolver(
            authentication=k8s_client.AuthenticationV1Api(cluster.api),
            audience="test-notifications",
            allowed_service_account_namespaces={SANDBOX_NAMESPACE},
        )
        action_api = actions_app(
            action_service,
            resolver,
            DisabledOperatorAuthenticator(),
            echo_catalog,
            callers=admitted_callers(owner, other),
            updates=ActionUpdateBroker("postgresql://unused-listener"),
            direct_wait_seconds=0,
            max_wait_seconds=30,
            reader_accounts=frozenset({delegate}),
        )
        history_wakeups = create_wakeups(make_url(sandbox_service_db_url))
        token_file = tmp_path / "delegate-token"
        token_file.write_text("delegate-token")
        async with (
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=action_api), base_url="http://actions.test"
            ) as action_http,
            authenticated_service(
                cluster,
                runner,
                token_file,
                database_url=sandbox_service_db_url,
                manager=delegate,
                token="delegate-token",
                audience="test-notifications",
            ) as (_, remote),
            ingest(sandbox_service_db_url, runner),
            history_service(sandbox_service_db_url, tmp_path / "history-token") as history,
            history_wakeups.listener.listen(),
        ):
            runner_token_file = tmp_path / "dispatcher-runner-token"
            runner_token_file.write_text("dispatcher-runner-token")
            cluster.fake.tokens["dispatcher-runner-token"] = TokenVerdict(
                username=f"system:serviceaccount:{delegate.namespace}:{delegate.name}",
                pod_name="notifications-runner-client",
                pod_uid="notifications-runner-client-uid",
                audiences=("test-app-sandbox-service",),
            )
            dispatcher_settings = DispatcherSettings(
                database_url=dispatcher_db_url,
                namespace=cluster.inventory.namespace,
                caller_accounts=frozenset({delegate}),
                token_audience="test-notifications",
                runner_control_target=remote.control.control_target,
                runner_token_file=runner_token_file,
                request_timeout_s=10,
                host="127.0.0.1",
                port=pick_free_port(),
                _cli_parse_args=False,
            )
            dispatcher = await stack.enter_async_context(
                running_dispatcher(
                    cluster, settings=dispatcher_settings, monkeypatch=monkeypatch, caller_token_file=token_file
                )
            )
            source = ActionServiceClient(action_http, token_file)
            service = Service(
                store,
                source,
                cluster.inventory,
                github=None,
                gateway=remote,
                history=history,
                dispatcher=dispatcher,
                notice_debounce=NoticeDebounceSettings(quiet_seconds=0),
                stale_confirmation_s=30,
                operator_reader_account=None,
            )
            api = create_app(service, resolver)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=api),
                base_url="http://notifications.test",
                headers={"Authorization": "Bearer owner-token"},
            ) as agent:
                destination = SandboxDestination(
                    owner=ServiceAccount(namespace=owner.namespace, name=owner.name),
                    sandbox=SANDBOX,
                    sandbox_uid=SANDBOX_UID,
                )
                native = remote.runner(destination)
                session_id = (await native.create(session_id=uuid4(), spec=MessageToDict(spec))).session_id

                async def history_caught_up() -> None:
                    # Delivery reads entries from the History Service, which copies them behind the runner.
                    (live,) = [session for session in await native.list_sessions() if session.session_id == session_id]
                    changed = asyncio.Event()
                    with history_wakeups.changes[Channel.HISTORY].subscribe(changed):
                        while (await history.read_session_events(session_id, limit=1)).last_cursor < live.last_cursor:
                            await changed.wait()
                            changed.clear()

                submitted = await action_http.post(
                    "/v1/action-requests",
                    headers={"Authorization": "Bearer owner-token"},
                    json={
                        "idempotency_key": "notification-test",
                        "title": "Notification test",
                        "action": {"group": "agentplane", "name": "echo"},
                        "arguments": {"text": "hello"},
                    },
                )
                submitted.raise_for_status()
                request = submitted.json()
                # Terminal before subscription exists: do not rely on a future source push.
                await action_service.decide(
                    UUID(request["id"]),
                    DecisionInput(
                        verdict=Verdict.DENY, expected_version=request["version"], idempotency_key="deny-test"
                    ),
                    OperatorPrincipal(issuer="test", subject="operator"),
                )
                body = {
                    "destination_ref": {"namespace": SANDBOX_NAMESPACE, "name": SANDBOX, "uid": SANDBOX_UID},
                    "session_id": session_id,
                    "source": {"provider": "actions", "request_id": request["id"]},
                    "idempotency_key": "listen",
                }
                assert (
                    await agent.post("/v1/subscriptions", json=body | {"owner": owner.model_dump()})
                ).status_code == 422
                assert (
                    await agent.post("/v1/subscriptions", json=body, headers={"Authorization": "Bearer other-token"})
                ).status_code == 404
                other_request = await action_http.post(
                    "/v1/action-requests",
                    headers={"Authorization": "Bearer other-token"},
                    json={
                        "idempotency_key": "other",
                        "title": "Other owner",
                        "action": {"group": "agentplane", "name": "echo"},
                        "arguments": {"text": "other"},
                    },
                )
                other_request.raise_for_status()
                # Destination is owned, source is not: the worker's broad read access is not inherited.
                assert (
                    await agent.post(
                        "/v1/subscriptions",
                        json=body | {"source": {"provider": "actions", "request_id": other_request.json()["id"]}},
                    )
                ).status_code == 403
                response = await agent.post("/v1/subscriptions", json=body)
                response.raise_for_status()
                subscription = response.json()
                assert subscription["idempotency_key"] == "listen"
                assert subscription["source"] == {
                    "provider": "actions",
                    "request_id": request["id"],
                    "after_sequence": 0,
                }
                assert "client_key" not in subscription
                assert (await agent.post("/v1/subscriptions", json=body)).json() == subscription
                initial = None
                if busy:
                    await remote.command(
                        destination,
                        session_id,
                        command_pb2.Command(
                            command_id="initial", submit_input=command_pb2.SubmitInput(text="Do other work")
                        ),
                    )
                    initial = await model.request()
                # Seed the real journal past one worker replay batch. The send path is exercised
                # through Dispatcher below; unrelated backlog entries need no Gateway RPCs.
                history_cursor = 0
                native_session = runner.sessions[session_id]
                for index in range(130):
                    entry = await native_session.journal.append(
                        event_pb2.HarnessStderr(text=f"notification journal seed {index + 1}")
                    )
                    history_cursor = entry.cursor
                    if history_cursor > 128:
                        break
                assert history_cursor > 128
                original = SessionDispatcherClient.submit_input
                lost = False
                notice_sent = False
                attempted_id: str | None = None

                async def lose_response(
                    self: SessionDispatcherClient, *, sandbox: str, session_id: str, command_id: str, text: str
                ) -> session_dispatcher_pb2.Dispatch:
                    nonlocal lost, notice_sent, attempted_id
                    if attempted_id is None:
                        attempted_id = command_id
                    else:
                        assert command_id == attempted_id
                    if failed_before_rpc and not lost:
                        lost = True
                        raise ValueError("simulated local failure before SubmitInput")
                    dispatch = await original(
                        self, sandbox=sandbox, session_id=session_id, command_id=command_id, text=text
                    )
                    notice_sent = True
                    if not lost:
                        lost = True
                        raise ConnectionError("simulated lost Dispatcher response")
                    return dispatch

                monkeypatch.setattr(SessionDispatcherClient, "submit_input", lose_response)

                async def step_until_sent() -> None:
                    # A step sends only once the History Service has every entry the runner had,
                    # and the runner can append between the two reads, so steps repeat until it sends.
                    while not notice_sent:
                        async with store.sessions.begin() as session:
                            await session.execute(
                                update(Inbox)
                                .where(Inbox.id == UUID(subscription["inbox_id"]))
                                .values(next_attempt=datetime.now(UTC))
                            )
                        await history_caught_up()
                        await service.step()

                if failed_before_rpc:
                    with pytest.raises(ValueError, match="before SubmitInput"):
                        await step_until_sent()
                else:
                    await step_until_sent()
                assert lost
                if initial is not None:
                    await model.reply(initial, Text("Initial work complete"))
                # The durable attempt marker exists, but the runner has not been called.
                await step_until_sent()
                notice_request = await model.request()
                notices = [text for text in notice_request.user_texts if text.startswith("Agentplane inbox notice: ")]
                assert len(notices) == 1
                hint = json.loads(notices[0].removeprefix("Agentplane inbox notice: "))
                assert attempted_id is not None
                assert hint == {
                    "inbox_id": subscription["inbox_id"],
                    "acknowledged_at_preparation": 0,
                    "through_at_preparation": 2,
                }
                assert "GET" not in notices[0]
                assert "acknowledgement" not in notices[0]
                await model.reply(notice_request, Text("Notifications received"))
                # A fresh service object has no in-memory delivery state to lean on.
                recovered = restart(service)
                inbox_id = UUID(hint["inbox_id"])
                page = None
                while page is None or not page["notice"]["confirmed"]:
                    await history_caught_up()
                    async with store.sessions.begin() as session:
                        await session.execute(
                            update(Inbox).where(Inbox.id == inbox_id).values(next_attempt=datetime.now(UTC))
                        )
                    await recovered.step()
                    response = await agent.get(
                        f"/v1/inboxes/{inbox_id}/entries",
                        params={"after_cursor": hint["acknowledged_at_preparation"], "limit": 128},
                    )
                    response.raise_for_status()
                    page = response.json()
                assert page is not None
                assert page["notice"]["command_id"] == attempted_id
                assert page["inbox"]["acknowledged"] == 0
                assert [entry["payload"]["state"] for entry in page["entries"]] == ["decision_pending", "denied"]
                assert [entry["event"] for entry in page["entries"]] == [
                    {"provider": "actions", "request_id": request["id"], "sequence": sequence} for sequence in [1, 2]
                ]
                assert (
                    await agent.get(f"/v1/inboxes/{inbox_id}/entries", headers={"Authorization": "Bearer other-token"})
                ).status_code == 404
                ack = await agent.put(
                    f"/v1/inboxes/{inbox_id}/acknowledgement", json={"through_cursor": page["entries"][0]["cursor"]}
                )
                assert ack.json()["acknowledged"] == 1
                remaining = await agent.get(f"/v1/inboxes/{inbox_id}/entries", params={"after_cursor": 1, "limit": 128})
                assert remaining.json()["inbox"]["acknowledged"] == 1
                assert [entry["cursor"] for entry in remaining.json()["entries"]] == [2]
                ack = await agent.put(
                    f"/v1/inboxes/{inbox_id}/acknowledgement", json={"through_cursor": page["entries"][-1]["cursor"]}
                )
                assert ack.json()["acknowledged"] == 2
                (native_session,) = runner.sessions.values()
                journal = await native_session.journal.since(0, limit=1000)
                admitted = [
                    entry.event.command_admitted.command.command_id
                    for entry in journal
                    if entry.event.HasField("command_admitted")
                ]
                assert admitted.count(page["notice"]["command_id"]) == 1
                await remote.command(
                    destination,
                    session_id,
                    command_pb2.Command(command_id="stop", stop_runner_session=command_pb2.StopRunnerSession()),
                )


if __name__ == "__main__":
    pytest_bazel.main()
