"""Real Actions → notification inbox → Sandbox Service → both native harnesses, no app."""

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import httpx
import pytest
import pytest_bazel
from google.protobuf.json_format import MessageToDict
from kubernetes_asyncio import client as k8s_client
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncEngine

from agentplane.action_service.api import create_app as actions_app
from agentplane.action_service.auth import DisabledOperatorAuthenticator
from agentplane.action_service.catalog import ActionCatalog
from agentplane.action_service.conftest import RecordingExecutor
from agentplane.action_service.db import ActionStore, make_sessionmaker
from agentplane.action_service.models import DecisionInput, OperatorPrincipal, Verdict
from agentplane.action_service.service import ActionService
from agentplane.action_service.test_fixtures.callers import admitted_callers
from agentplane.action_service.updates import ActionUpdates
from agentplane.notification_service.api import create_app
from agentplane.notification_service.db import Inbox
from agentplane.notification_service.service import Service
from agentplane.notification_service.settings import NoticeDebounceSettings
from agentplane.notification_service.sources.actions import Actions
from agentplane.notification_service.store import Store
from agentplane.protocol import command_pb2, event_log_pb2
from agentplane.runner import protocol_pb2
from agentplane.runner.conftest import RunnerHandle
from agentplane.runner.testing.scripted_model import ScriptedModel, Text
from agentplane.sandbox_service.client import Runner
from agentplane.sandbox_service.instructions import render_platform_instructions
from agentplane.sandbox_service.protocol_pb2 import SandboxDestination, ServiceAccount
from agentplane.sandbox_service.testing.kubernetes import (
    ACCOUNT,
    SANDBOX,
    SANDBOX_UID,
    authenticated_service,
    kubernetes,
)
from agentplane.subjects import ServiceAccountRef
from agentplane.testing.fake_apiserver import SANDBOX_NAMESPACE, TokenVerdict
from agentplane.workload_auth.principal import WorkloadPrincipalResolver

# gazelle:include_dep @pypi//protobuf


@pytest.mark.parametrize("busy", [False, True])
async def test_listen_deliver_read_ack_and_recover_lost_response_without_app(
    store: Store,
    engine: AsyncEngine,
    action_engine: AsyncEngine,
    echo_catalog: ActionCatalog,
    echo_executor: RecordingExecutor,
    runner: RunnerHandle,
    spec: protocol_pb2.SessionSpec,
    model: ScriptedModel,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    busy: bool,
) -> None:
    owner = ServiceAccountRef(namespace=SANDBOX_NAMESPACE, name=ACCOUNT)
    delegate = ServiceAccountRef(namespace=SANDBOX_NAMESPACE, name="notifications")
    other = ServiceAccountRef(namespace=SANDBOX_NAMESPACE, name="other")
    action_service = ActionService(
        ActionStore(make_sessionmaker(action_engine)), echo_catalog, {"agentplane": echo_executor}
    )
    await action_service.start()
    try:
        async with kubernetes() as cluster, asyncio.timeout(60):
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
                updates=ActionUpdates("postgresql://unused-listener"),
                direct_wait_seconds=0,
                max_wait_seconds=30,
                reader_accounts=frozenset({delegate}),
            )
            token_file = tmp_path / "delegate-token"
            token_file.write_text("delegate-token")
            async with (
                httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=action_api), base_url="http://actions.test"
                ) as action_http,
                authenticated_service(
                    cluster,
                    runner.port,
                    token_file,
                    manager=delegate,
                    token="delegate-token",
                    audience="test-notifications",
                    platform_instructions=render_platform_instructions(
                        egress_api_url="http://egress.test",
                        actions_service_url="http://actions.test",
                        notifications_service_url="http://notifications.test",
                    ),
                ) as remote,
            ):
                source = Actions(action_http, token_file)
                service = Service(store, source, remote, notice_debounce=NoticeDebounceSettings(quiet_seconds=0))
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
                    await native.open("notifications", MessageToDict(spec))
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
                        "session_id": "notifications",
                        "source": {"provider": "actions", "request_id": request["id"]},
                        "idempotency_key": "listen",
                    }
                    assert (
                        await agent.post("/v1/subscriptions", json=body | {"owner": owner.model_dump()})
                    ).status_code == 422
                    assert (
                        await agent.post(
                            "/v1/subscriptions", json=body, headers={"Authorization": "Bearer other-token"}
                        )
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
                        await native.command(
                            "notifications",
                            command_pb2.Command(
                                command_id="initial", submit_input=command_pb2.SubmitInput(text="Do other work")
                            ),
                            after_cursor=0,
                        )
                        initial = await model.request()
                    # Real native journal longer than a worker replay batch. First delivery must
                    # not spend claims scanning it, even when the admission response is lost.
                    history_cursor = 0
                    for index in range(130):
                        receipt = await native.command(
                            "notifications",
                            command_pb2.Command(
                                command_id=f"old-{index}",
                                interrupt_turn=command_pb2.InterruptTurn(turn_id="nonexistent-turn"),
                            ),
                            after_cursor=history_cursor,
                        )
                        history_cursor = receipt.cursor
                    assert history_cursor > 128
                    original = Runner.command
                    lost = False

                    async def lose_response(
                        self: Runner, session_id: str, command: command_pb2.Command, *, after_cursor: int
                    ) -> event_log_pb2.EventEntry:
                        nonlocal lost
                        if command.HasField("submit_input"):
                            assert after_cursor >= history_cursor
                        receipt = await original(self, session_id, command, after_cursor=after_cursor)
                        if not lost and command.HasField("submit_input"):
                            lost = True
                            raise ConnectionError("simulated lost admission response")
                        return receipt

                    monkeypatch.setattr(Runner, "command", lose_response)
                    await service.step()
                    assert lost
                    if initial is not None:
                        await model.reply(initial, Text("Initial work complete"))
                    notice_request = await model.request()
                    assert any("Agentplane automated notification" in text for text in notice_request.user_texts)
                    assert "/v1/subscriptions" in notice_request.system_text
                    assert SANDBOX_UID in notice_request.system_text
                    await model.reply(notice_request, Text("Notifications received"))
                    # A fresh service object has no in-memory delivery state to lean on.
                    recovered = Service(Store(engine), source, remote, notice_debounce=service.notice_debounce)
                    inbox_id = UUID(subscription["inbox_id"])
                    page = None
                    for _ in range(10):
                        async with store.sessions.begin() as session:
                            await session.execute(
                                update(Inbox).where(Inbox.id == inbox_id).values(next_attempt=datetime.now(UTC))
                            )
                        await recovered.step()
                        response = await agent.get(f"/v1/inboxes/{inbox_id}/entries")
                        response.raise_for_status()
                        page = response.json()
                        if page["notice"]["confirmed"]:
                            break
                    assert page is not None
                    assert page["notice"]["confirmed"]
                    assert page["inbox"]["acknowledged"] == 0
                    assert [entry["payload"]["state"] for entry in page["entries"]] == ["decision_pending", "denied"]
                    assert [entry["event"] for entry in page["entries"]] == [
                        {"provider": "actions", "request_id": request["id"], "sequence": sequence}
                        for sequence in [1, 2]
                    ]
                    assert (
                        await agent.get(
                            f"/v1/inboxes/{inbox_id}/entries", headers={"Authorization": "Bearer other-token"}
                        )
                    ).status_code == 404
                    ack = await agent.put(
                        f"/v1/inboxes/{inbox_id}/acknowledgement",
                        json={"through_cursor": page["entries"][-1]["cursor"]},
                    )
                    assert ack.json()["acknowledged"] == 2
                    journal = await runner.runner.sessions["notifications"].journal.since(0, limit=1000)
                    admitted = [
                        entry.event.command_admitted.command.command_id
                        for entry in journal
                        if entry.event.HasField("command_admitted")
                    ]
                    assert admitted.count(page["notice"]["command_id"]) == 1
                    await native.command(
                        "notifications",
                        command_pb2.Command(command_id="stop", stop_runner_session=command_pb2.StopRunnerSession()),
                        after_cursor=0,
                    )
    finally:
        await action_service.close()


if __name__ == "__main__":
    pytest_bazel.main()
