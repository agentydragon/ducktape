"""Real Actions → notification inbox → Dispatcher/Runner Gateway and History → both harnesses, no app."""

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import AsyncExitStack
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import grpc
import httpx
import pytest
import pytest_bazel
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
from agentplane.grpc.calls import bearer, dial
from agentplane.history_service.channels import Channel, create_wakeups
from agentplane.history_service.testing.backend import history_service, ingest
from agentplane.notification_service.api import create_app
from agentplane.notification_service.db import Inbox
from agentplane.notification_service.dispatcher_client import SessionDispatcherClient
from agentplane.notification_service.service import Service
from agentplane.notification_service.settings import NoticeDebounceSettings
from agentplane.notification_service.sources.actions import ActionServiceClient
from agentplane.notification_service.store import Store
from agentplane.proto import event_pb2, runner_pb2, session_dispatcher_pb2
from agentplane.proto.sandboxes_pb2 import SandboxDestination, ServiceAccount
from agentplane.runner.config import RunnerConfig
from agentplane.runner.platform_instructions import render_platform_instructions
from agentplane.runner.runner import Runner
from agentplane.runner.sandbox_launch import SandboxIdentity
from agentplane.runner.testing.scripted_model import ScriptedModel, Text
from agentplane.runner_gateway.testing.gateway_process import run_gateway
from agentplane.sandboxes.testing.api_server import NAMESPACE, SANDBOX, SandboxCluster, sandbox_cluster
from agentplane.subjects import ServiceAccountRef
from agentplane.workload_auth.principal import WorkloadPrincipalResolver
from util.testing.postgres import DatabaseNames, create_temporary_database

# gazelle:include_dep @pypi//protobuf


@pytest.fixture
def dispatcher_db_url(postgres_admin_url: str, database_names: DatabaseNames) -> Iterator[str]:
    with create_temporary_database(postgres_admin_url, database_names()) as url:
        yield url


@pytest.fixture
def config(config: RunnerConfig, cluster: SandboxCluster) -> RunnerConfig:
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
            sandbox=SandboxIdentity(namespace=NAMESPACE, name=SANDBOX, uid=cluster.uid),
            platform_instructions=platform_instructions,
        ),
    )


@pytest.fixture
async def cluster() -> AsyncIterator[SandboxCluster]:
    async with sandbox_cluster() as cluster:
        yield cluster


async def test_listen_deliver_read_ack_and_recover_lost_response_without_app(
    store: Store,
    cluster: SandboxCluster,
    restart: Callable[[Service], Service],
    action_engine: AsyncEngine,
    echo_catalog: ActionCatalog,
    echo_executor: RecordingExecutor,
    runner: Runner,
    spec: runner_pb2.SessionSpec,
    model: ScriptedModel,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
    sandbox_service_db_url: str,
    dispatcher_db_url: str,
) -> None:
    caplog.set_level(logging.INFO, logger=__name__)
    owner = cluster.owner
    delegate = ServiceAccountRef(namespace=NAMESPACE, name="notifications")
    other = ServiceAccountRef(namespace=NAMESPACE, name="other")
    action_service = ActionService(
        action_store(make_sessionmaker(action_engine)),
        echo_catalog,
        {"agentplane": echo_executor},
        dispatches=ChangeSignal(),
    )
    async with action_service, asyncio.timeout(60), AsyncExitStack() as stack:
        owner_token = await cluster.server.pod_token(NAMESPACE, SANDBOX, ["test-notifications"])
        delegate_token = await cluster.server.pod_token(
            NAMESPACE, "notifications", ["test-notifications", "test-dispatcher", "test-gateway"]
        )
        other_token = await cluster.server.pod_token(NAMESPACE, "other", ["test-notifications"])
        runner_token = await cluster.server.pod_token(NAMESPACE, SANDBOX, ["test-runner-control"])
        resolver = WorkloadPrincipalResolver(
            authentication=k8s_client.AuthenticationV1Api(cluster.server.api),
            audience="test-notifications",
            allowed_service_account_namespaces={NAMESPACE},
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
        token_file.write_text(delegate_token)
        gateway = await stack.enter_async_context(
            run_gateway(
                cluster.server.kubeconfig,
                database_url=dispatcher_db_url,
                namespace=cluster.inventory.namespace,
                dispatcher_callers=(delegate,),
                gateway_callers=(delegate,),
                dispatcher_audience="test-dispatcher",
                gateway_audience="test-gateway",
                runner_audience="test-runner-control",
                runner_proxy_token_file=token_file,
            )
        )
        remote = gateway.gateway_client(token_file, request_timeout_s=20, lifecycle_timeout_s=310)
        destination = SandboxDestination(
            owner=ServiceAccount(namespace=owner.namespace, name=owner.name), sandbox=SANDBOX, sandbox_uid=cluster.uid
        )
        runner_connections = await stack.enter_async_context(AsyncExitStack())
        await runner_connections.enter_async_context(
            gateway.connected_runner(runner, token=runner_token, proxy_client=remote, destination=destination)
        )
        dispatcher_channel = await stack.enter_async_context(dial(gateway.target))
        dispatcher = SessionDispatcherClient(dispatcher_channel, token_file=token_file, request_timeout_s=10)
        # Sharing a listener does not let a Runner-audience credential call Dispatcher methods.
        with pytest.raises(grpc.aio.AioRpcError) as rejected:
            await dispatcher.stub.ListDispatches(
                session_dispatcher_pb2.ListDispatchesRequest(),
                metadata=(("authorization", f"Bearer {runner_token}"),),
                timeout=5,
            )
        assert rejected.value.code() is grpc.StatusCode.UNAUTHENTICATED
        async with (
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=action_api), base_url="http://actions.test"
            ) as action_http,
            ingest(sandbox_service_db_url, runner),
            history_service(sandbox_service_db_url, tmp_path / "history-token") as history,
            history_wakeups.listener.listen(),
        ):
            source = ActionServiceClient(action_http, token_file)
            service = Service(
                store,
                source,
                cluster.inventory,
                github=None,
                control=remote,
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
                headers={"Authorization": f"Bearer {owner_token}"},
            ) as agent:
                native = remote.runner(destination)
                session_id = (await native.open_session(session_id=uuid4(), spec=spec)).session_id
                native_session = runner.sessions[session_id]

                async def history_caught_up() -> None:
                    # Delivery reads entries from History, which copies them behind the native journal.
                    live_cursor = native_session.journal.last_cursor
                    changed = asyncio.Event()
                    with history_wakeups.changes[Channel.HISTORY].subscribe(changed):
                        while (await history.read_session_events(session_id, limit=1)).last_cursor < live_cursor:
                            await changed.wait()
                            changed.clear()

                submitted = await action_http.post(
                    "/v1/action-requests",
                    headers={"Authorization": f"Bearer {owner_token}"},
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
                    "destination_ref": {"namespace": NAMESPACE, "name": SANDBOX, "uid": cluster.uid},
                    "session_id": session_id,
                    "source": {"provider": "actions", "request_id": request["id"]},
                    "idempotency_key": "listen",
                }
                assert (
                    await agent.post("/v1/subscriptions", json=body | {"owner": owner.model_dump()})
                ).status_code == 422
                assert (
                    await agent.post("/v1/subscriptions", json=body, headers={"Authorization": f"Bearer {other_token}"})
                ).status_code == 404
                other_request = await action_http.post(
                    "/v1/action-requests",
                    headers={"Authorization": f"Bearer {other_token}"},
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
                # Seed through the runner's serialized event path, not a test-owned journal batch
                # that can collide with the stdout reader's batch. The Dispatcher send path below
                # exercises a replay after one worker batch; these unrelated events need no RPCs.
                history_cursor = native_session.journal.last_cursor
                for index in range(130):
                    entry = await native_session.emit(
                        event_pb2.HarnessStderr(text=f"notification journal seed {index + 1}"), sources=[]
                    )
                    history_cursor = entry.cursor
                    if history_cursor > 128:
                        break
                assert history_cursor > 128
                # Subscription preflight requires the live Runner. Disconnect it only after that
                # succeeds, so the committed notice remains queued during the uncertain retry.
                await runner_connections.aclose()
                original = SessionDispatcherClient.submit_input
                committed_responses: list[session_dispatcher_pb2.Dispatch] = []
                attempted_ids: list[str] = []
                attempted_id: str | None = None

                async def lose_response(
                    self: SessionDispatcherClient, *, sandbox: str, session_id: str, command_id: str, text: str
                ) -> session_dispatcher_pb2.Dispatch:
                    nonlocal attempted_id
                    if attempted_id is None:
                        attempted_id = command_id
                    else:
                        assert command_id == attempted_id
                    attempted_ids.append(command_id)
                    dispatch = await original(
                        self, sandbox=sandbox, session_id=session_id, command_id=command_id, text=text
                    )
                    committed_responses.append(dispatch)
                    if len(committed_responses) == 1:
                        raise ConnectionError("simulated lost Dispatcher response")
                    return dispatch

                monkeypatch.setattr(SessionDispatcherClient, "submit_input", lose_response)

                async def step_until_submit_count(target: int) -> None:
                    # Retry the same durable notice ID after losing a committed RPC response.
                    async with asyncio.timeout(20):
                        while len(committed_responses) < target:
                            async with store.sessions.begin() as session:
                                await session.execute(
                                    update(Inbox)
                                    .where(Inbox.id == UUID(subscription["inbox_id"]))
                                    .values(next_attempt=datetime.now(UTC))
                                )
                            await history_caught_up()
                            await service.step()

                async def wait_for_runner_admission() -> None:
                    assert attempted_id is not None
                    last_state = "not submitted"
                    last_reason = ""
                    watch = dispatcher.stub.WatchDispatches(
                        session_dispatcher_pb2.WatchDispatchesRequest(
                            filter=session_dispatcher_pb2.DispatchFilter(session_id=session_id)
                        ),
                        metadata=await bearer(dispatcher.token_file),
                        timeout=5,
                    )
                    try:
                        async with asyncio.timeout(5):
                            async for response in watch:
                                dispatch = response.dispatch
                                if dispatch.command_id != attempted_id:
                                    continue
                                last_state = session_dispatcher_pb2.DeliveryState.Name(dispatch.state)
                                last_reason = dispatch.reason if dispatch.HasField("reason") else ""
                                if dispatch.state == session_dispatcher_pb2.DELIVERY_STATE_RUNNER_ADMITTED:
                                    return
                                assert dispatch.state not in {
                                    session_dispatcher_pb2.DELIVERY_STATE_NO_OP,
                                    session_dispatcher_pb2.DELIVERY_STATE_FAILED,
                                    session_dispatcher_pb2.DELIVERY_STATE_CANCELLED_IN_DISPATCHER,
                                    session_dispatcher_pb2.DELIVERY_STATE_UNKNOWN,
                                }, f"Dispatcher terminated notification delivery: {last_state}: {last_reason}"
                        raise AssertionError(
                            f"Dispatcher WatchDispatches ended before admission: {last_state}: {last_reason}"
                        )
                    except TimeoutError as error:
                        raise AssertionError(
                            f"Dispatcher did not admit notification: {last_state}: {last_reason}"
                        ) from error
                    finally:
                        watch.cancel()

                await step_until_submit_count(1)
                first_id = committed_responses[0].command_id
                assert first_id == attempted_id
                assert len(attempted_ids) == 1
                # The Dispatcher RPC committed the first request, but the notification caller lost its response.
                await step_until_submit_count(2)
                assert len(attempted_ids) == 2
                assert attempted_ids == [first_id, first_id]
                assert [response.command_id for response in committed_responses] == [first_id, first_id]
                assert committed_responses[1].state in {
                    session_dispatcher_pb2.DELIVERY_STATE_QUEUED_IN_DISPATCHER,
                    session_dispatcher_pb2.DELIVERY_STATE_DELIVERING_TO_RUNNER,
                    session_dispatcher_pb2.DELIVERY_STATE_RUNNER_ADMITTED,
                }
                assert committed_responses[1].created_at == committed_responses[0].created_at
                await stack.enter_async_context(
                    gateway.connected_runner(runner, token=runner_token, proxy_client=remote, destination=destination)
                )
                await wait_for_runner_admission()
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
                    await agent.get(
                        f"/v1/inboxes/{inbox_id}/entries", headers={"Authorization": f"Bearer {other_token}"}
                    )
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
                await native_session.shutdown()


if __name__ == "__main__":
    pytest_bazel.main()
