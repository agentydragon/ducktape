"""Actual MCP HTTP protocol, destination workload validation, and canonical Action receipts."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import timedelta
from unittest.mock import AsyncMock
from uuid import UUID

import httpx
import pytest
import pytest_bazel
from fastapi import FastAPI
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport
from kubernetes_asyncio import client as k8s_client
from kubernetes_asyncio.client import AuthenticationV1Api, CoreV1Api
from sqlalchemy.ext.asyncio import AsyncEngine
from starlette.types import Message, Scope

from x.agentplane.action_service.api import create_app
from x.agentplane.action_service.auth import DisabledOperatorAuthenticator, workload_principal
from x.agentplane.action_service.catalog import ActionCatalog, ActionDefinition, ActionGroup, McpExecutorBinding
from x.agentplane.action_service.client import WORKLOAD_CREDENTIAL_PLACEHOLDER
from x.agentplane.action_service.db import ActionStore, make_sessionmaker
from x.agentplane.action_service.models import (
    ActionRequestView,
    ActionState,
    CancellationOutcome,
    CancellationResult,
    DecisionInput,
    Executor,
    Principal,
    PrincipalRole,
    Verdict,
)
from x.agentplane.action_service.service import ActionService
from x.agentplane.action_service.updates import ActionUpdates
from x.agentplane.sandbox_auth.http import SandboxPrincipalAuthenticator
from x.agentplane.sandbox_auth.principal import (
    POD_NAME_CLAIM,
    POD_UID_CLAIM,
    SandboxPrincipal,
    SandboxPrincipalResolver,
)

AUDIENCE = "test-action-audience"
NAMESPACE = "test-action-sandboxes"
OPERATOR = Principal(issuer="test", subject="operator", role=PrincipalRole.OPERATOR)


def sandbox(label: str) -> SandboxPrincipal:
    return SandboxPrincipal(
        namespace=NAMESPACE,
        service_account_name="test-runner",
        service_account_subject=f"system:serviceaccount:{NAMESPACE}:test-runner",
        pod_name=f"test-pod-{label}",
        pod_uid=f"test-pod-uid-{label}",
        sandbox_name=f"test-sandbox-{label}",
        sandbox_uid=f"test-sandbox-uid-{label}",
    )


class EgressSubstitution(httpx.AsyncBaseTransport):
    """The runner supplies only a public placeholder; substitution is an external boundary."""

    def __init__(self, app: FastAPI, token: str) -> None:
        self._upstream = httpx.ASGITransport(app)
        self._token = token

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == f"Bearer {WORKLOAD_CREDENTIAL_PLACEHOLDER}"
        request.headers["authorization"] = f"Bearer {self._token}"
        return await self._upstream.handle_async_request(request)

    async def aclose(self) -> None:
        await self._upstream.aclose()


@dataclass
class Frontend:
    app: FastAPI
    store: ActionStore
    service: ActionService
    authentication: AsyncMock
    core: AsyncMock
    updates: ActionUpdates
    tokens: dict[str, SandboxPrincipal]

    def client(self, token: str = "test-token-a", *, egress: bool = False) -> Client[StreamableHttpTransport]:
        def factory(
            headers: dict[str, str] | None = None,
            timeout: httpx.Timeout | None = None,
            auth: httpx.Auth | None = None,
            *,
            follow_redirects: bool = True,
        ) -> httpx.AsyncClient:
            transport = EgressSubstitution(self.app, token) if egress else httpx.ASGITransport(self.app)
            return httpx.AsyncClient(
                transport=transport,
                headers=headers,
                timeout=timeout or httpx.Timeout(30),
                auth=auth,
                follow_redirects=follow_redirects,
            )

        return Client(
            StreamableHttpTransport(
                "http://actions.test/mcp",
                headers={"Authorization": f"Bearer {WORKLOAD_CREDENTIAL_PLACEHOLDER if egress else token}"},
                httpx_client_factory=factory,
            )
        )


@pytest.fixture
async def frontend(engine: AsyncEngine, db_url: str, echo_executor: Executor) -> AsyncIterator[Frontend]:
    tokens = {f"test-token-{label}": sandbox(label) for label in ("a", "b")}
    authentication = AsyncMock(spec=AuthenticationV1Api)
    core = AsyncMock(spec=CoreV1Api)

    async def review(body: k8s_client.V1TokenReview) -> k8s_client.V1TokenReview:
        identity = tokens.get(body.spec.token)
        return k8s_client.V1TokenReview(
            spec=body.spec,
            status=k8s_client.V1TokenReviewStatus(
                authenticated=identity is not None,
                audiences=[AUDIENCE],
                user=k8s_client.V1UserInfo(
                    username=identity.service_account_subject,
                    extra={POD_NAME_CLAIM: [identity.pod_name], POD_UID_CLAIM: [identity.pod_uid]},
                )
                if identity is not None
                else None,
            ),
        )

    async def pod(name: str, namespace: str) -> k8s_client.V1Pod:
        identity = next(identity for identity in tokens.values() if identity.pod_name == name)
        return k8s_client.V1Pod(
            metadata=k8s_client.V1ObjectMeta(
                name=name,
                namespace=namespace,
                uid=identity.pod_uid,
                owner_references=[
                    k8s_client.V1OwnerReference(
                        api_version="agents.x-k8s.io/v1beta1",
                        kind="Sandbox",
                        controller=True,
                        name=identity.sandbox_name,
                        uid=identity.sandbox_uid,
                    )
                ],
            )
        )

    authentication.create_token_review = AsyncMock(side_effect=review)
    core.read_namespaced_pod = AsyncMock(side_effect=pod)
    catalog = ActionCatalog(
        groups={
            "test-group": ActionGroup(
                title="Test group",
                description="test-group-description-not-default",
                executor=McpExecutorBinding(
                    description="test-executor-description-not-default", config={"token": "test-backend-secret"}
                ),
                actions={
                    name: ActionDefinition(
                        description="test-full-description-" + "x" * 10000,
                        input_schema={
                            "type": "object",
                            "properties": {"message": {"type": "string"}},
                            "required": ["message"],
                            "additionalProperties": False,
                        },
                    )
                    for name in ("alpha", "beta")
                },
            )
        }
    )
    store = ActionStore(make_sessionmaker(engine))
    service = ActionService(store, catalog, {"test-group": echo_executor})
    updates = ActionUpdates(db_url)
    app = create_app(
        service,
        SandboxPrincipalAuthenticator(
            SandboxPrincipalResolver(
                authentication=authentication,
                core_v1=core,
                audience=AUDIENCE,
                allowed_service_account_namespaces=frozenset({NAMESPACE}),
            )
        ),
        DisabledOperatorAuthenticator(),
        catalog,
        updates=updates,
    )
    # pytest-asyncio resumes yield-fixture teardown in another task. The MCP lifespan's
    # AnyIO scopes must enter and exit in the same task, as they do under uvicorn.
    started: asyncio.Future[None] = asyncio.get_running_loop().create_future()
    stopping = asyncio.Event()

    async def lifespan() -> None:
        try:
            async with app.router.lifespan_context(app):
                started.set_result(None)
                await stopping.wait()
        except BaseException as error:
            if not started.done():
                started.set_exception(error)
            raise

    task = asyncio.create_task(lifespan())
    try:
        await started
        yield Frontend(app, store, service, authentication, core, updates, tokens)
    finally:
        stopping.set()
        await task
        await service.close()


async def test_compact_catalog_opt_in_pagination_and_small_generic_schema(frontend: Frontend) -> None:
    async with frontend.client(egress=True) as client:
        tools = await client.list_tools()
        assert len(tools) == 6
        cancellation = next(tool for tool in tools if tool.name == "cancel_action_request")
        assert set(cancellation.inputSchema["properties"]) == {"request_id"}
        assert cancellation.inputSchema["required"] == ["request_id"]
        assert all("args" not in tool.inputSchema["properties"] for tool in tools)
        assert "test-full-description" not in " ".join(tool.model_dump_json() for tool in tools)
        page = await client.call_tool("list_actions", {"limit": 1})
        assert page.structured_content == {
            "actions": [{"group": "test-group", "name": "alpha", "available": True}],
            "next_after": {"group": "test-group", "name": "alpha"},
        }
        next_page = await client.call_tool("list_actions", {"limit": 1, "after": page.structured_content["next_after"]})
        assert next_page.structured_content == {"actions": [{"group": "test-group", "name": "beta", "available": True}]}
        details = await client.call_tool(
            "get_action", {"group": "test-group", "name": "alpha", "include_fields": ["input_schema"]}
        )
        assert details.structured_content is not None
        assert "input_schema" in details.structured_content
        assert "description" not in details.structured_content
        assert "test-backend-secret" not in str(details)
        unsupported = await client.call_tool(
            "get_action",
            {"group": "test-group", "name": "alpha", "include_fields": ["output_schema"]},
            raise_on_error=False,
        )
        assert unsupported.is_error


async def test_submission_wait_receipts_events_and_owner_scope(frontend: Frontend) -> None:
    async with frontend.client() as caller, frontend.client("test-token-b") as other:
        envelope = {
            "idempotency_key": "test-submit",
            "action": {"group": "test-group", "name": "alpha"},
            "arguments": {"message": "test-message"},
        }
        result = await caller.call_tool("request_action", {"request": envelope})
        receipt = ActionRequestView.model_validate(result.structured_content)
        assert receipt.state is ActionState.DECISION_PENDING
        repeated = await caller.call_tool("request_action", {"request": envelope})
        assert repeated.structured_content == result.structured_content
        for name in ("get_action_request", "list_action_request_events"):
            denied = await other.call_tool(name, {"request_id": str(receipt.id)}, raise_on_error=False)
            assert denied.is_error
        task = asyncio.create_task(
            caller.call_tool("get_action_request", {"request_id": str(receipt.id), "wait_seconds": 10})
        )
        # A commit before or after subscription must both be observed, without polling.
        await frontend.store.decide(
            receipt.id,
            DecisionInput(verdict=Verdict.DENY, expected_version=1, idempotency_key="test-deny"),
            OPERATOR,
            provider="test-human",
        )
        assert ActionRequestView.model_validate((await task).structured_content).state is ActionState.DENIED
        first = await caller.call_tool("list_action_request_events", {"request_id": str(receipt.id), "limit": 1})
        assert first.structured_content is not None
        assert first.structured_content["next_after_sequence"] == 1
        second = await caller.call_tool(
            "list_action_request_events", {"request_id": str(receipt.id), "after_sequence": 1}
        )
        assert second.structured_content is not None
        assert second.structured_content["events"][0]["state"] == "denied"
        assert "next_after_sequence" not in second.structured_content
        assert (
            await frontend.store.get(receipt.id, workload_principal(frontend.tokens["test-token-a"]))
        ).id == receipt.id


@pytest.mark.parametrize("authorization", [None, "Bearer test-operator", f"Bearer {WORKLOAD_CREDENTIAL_PLACEHOLDER}"])
async def test_transport_requires_real_workload_bearer(frontend: Frontend, authorization: str | None) -> None:
    async with httpx.AsyncClient(transport=httpx.ASGITransport(frontend.app), base_url="http://actions.test") as client:
        headers = {"Authorization": authorization} if authorization is not None else {}
        for method in ("GET", "POST", "DELETE"):
            response = await client.request(method, "/mcp", headers=headers)
            assert response.status_code == 401


async def test_revalidates_live_pod_and_rejects_duplicate_auth_and_forgery(frontend: Frontend) -> None:
    async with frontend.client() as client:
        invalid = await client.call_tool(
            "request_action",
            {
                "request": {
                    "idempotency_key": "test-forgery",
                    "action": {"group": "test-group", "name": "alpha"},
                    "arguments": {"message": "test"},
                    "caller_principal": "test-other",
                }
            },
            raise_on_error=False,
        )
        assert invalid.is_error
        assert await frontend.store.list_requests(OPERATOR) == []
        frontend.core.read_namespaced_pod.side_effect = k8s_client.ApiException(status=404)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(frontend.app), base_url="http://actions.test") as http:
        revoked = await http.post(
            "/mcp",
            headers={"Authorization": "Bearer test-token-a"},
            json={"jsonrpc": "2.0", "id": 99, "method": "tools/list"},
        )
        assert revoked.status_code == 401
        duplicate = await http.post(
            "/mcp", headers=[("Authorization", "Bearer test-token-a"), ("Authorization", "Bearer test-token-b")]
        )
        assert duplicate.status_code == 401


async def test_origin_is_not_categorically_rejected_and_loopback_guard_stays_active(frontend: Frontend) -> None:
    async with httpx.AsyncClient(transport=httpx.ASGITransport(frontend.app), base_url="http://127.0.0.1") as http:
        headers = {
            "Authorization": "Bearer test-token-a",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": "2025-11-25",
            "Origin": "http://127.0.0.1",
        }
        body = {"jsonrpc": "2.0", "id": 99, "method": "tools/list"}
        assert (await http.post("/mcp", headers=headers, json=body)).status_code == 200
        assert (
            await http.post("/mcp", headers={**headers, "Origin": "https://test-untrusted.example"}, json=body)
        ).status_code == 403
        assert (
            await http.post("/mcp", headers={**headers, "Host": "test-rebinding.example"}, json=body)
        ).status_code == 421


async def test_submission_deadline_and_wait_validation(frontend: Frontend) -> None:
    async with frontend.client() as client:
        request = {
            "idempotency_key": "test-bounded-submit",
            "action": {"group": "test-group", "name": "alpha"},
            "arguments": {"message": "test-wait"},
        }
        receipt = ActionRequestView.model_validate(
            (await client.call_tool("request_action", {"request": request, "wait_seconds": 0.001})).structured_content
        )
        assert receipt.state is ActionState.DECISION_PENDING
        assert not frontend.updates._subscribers
        for seconds in (-1, 31):
            invalid = await client.call_tool(
                "request_action",
                {"request": {**request, "idempotency_key": "test-invalid"}, "wait_seconds": seconds},
                raise_on_error=False,
            )
            assert invalid.is_error
        assert len(await frontend.store.list_requests(OPERATOR)) == 1


async def test_allowed_action_executes_and_returns_canonical_result(frontend: Frontend) -> None:
    async with frontend.client() as client:
        result = await client.call_tool(
            "request_action",
            {
                "request": {
                    "idempotency_key": "test-execute",
                    "action": {"group": "test-group", "name": "alpha"},
                    "arguments": {"message": "test-result"},
                }
            },
        )
        receipt = ActionRequestView.model_validate(result.structured_content)
        await frontend.service.decide(
            receipt.id,
            DecisionInput(verdict=Verdict.ALLOW, expected_version=receipt.version, idempotency_key="test-allow"),
            OPERATOR,
        )
        finished = ActionRequestView.model_validate(
            (
                await client.call_tool("get_action_request", {"request_id": str(receipt.id), "wait_seconds": 10})
            ).structured_content
        )
        assert finished.state is ActionState.SUCCEEDED
        assert finished.execution is not None
        assert finished.execution.result == {"echo": {"message": "test-result"}}


@dataclass
class WaitSignals:
    registered: asyncio.Event
    released: asyncio.Event


@pytest.fixture
def subscription_signals(frontend: Frontend, monkeypatch: pytest.MonkeyPatch) -> WaitSignals:
    signals = WaitSignals(asyncio.Event(), asyncio.Event())
    subscribe = frontend.updates.subscribe

    @contextmanager
    def observed_subscription(request_id: UUID) -> Iterator[asyncio.Event]:
        with subscribe(request_id) as changed:
            signals.registered.set()
            try:
                yield changed
            finally:
                signals.released.set()

    monkeypatch.setattr(frontend.updates, "subscribe", observed_subscription)
    return signals


async def test_http_disconnect_releases_wait_without_cancelling_action(
    frontend: Frontend, subscription_signals: WaitSignals
) -> None:
    incoming: asyncio.Queue[Message] = asyncio.Queue()
    incoming.put_nowait(
        {
            "type": "http.request",
            "body": json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {
                        "name": "request_action",
                        "arguments": {
                            "request": {
                                "idempotency_key": "test-disconnect",
                                "action": {"group": "test-group", "name": "alpha"},
                                "arguments": {"message": "test-disconnect"},
                            },
                            "wait_seconds": 30,
                        },
                    },
                }
            ).encode(),
            "more_body": False,
        }
    )
    scope: Scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/mcp",
        "raw_path": b"/mcp",
        "query_string": b"",
        "root_path": "",
        "headers": [
            (b"host", b"actions.test"),
            (b"authorization", b"Bearer test-token-a"),
            (b"content-type", b"application/json"),
            (b"accept", b"application/json, text/event-stream"),
            (b"mcp-protocol-version", b"2025-11-25"),
        ],
        "server": ("actions.test", 80),
        "client": ("127.0.0.1", 12345),
    }

    async def send(message: Message) -> None:
        pass

    async with asyncio.timeout(10):
        request_task = asyncio.create_task(frontend.app(scope, incoming.get, send))
        await subscription_signals.registered.wait()
        incoming.put_nowait({"type": "http.disconnect"})
        await request_task
        await subscription_signals.released.wait()
    requests = await frontend.store.list_requests(OPERATOR)
    assert len(requests) == 1
    assert requests[0].state is ActionState.DECISION_PENDING
    assert not frontend.updates._subscribers


@pytest.mark.parametrize(
    "state", [ActionState.DECISION_PENDING, ActionState.ALLOWED, ActionState.DISPATCHING, ActionState.DENIED]
)
async def test_cancellation_preserves_canonical_cutoff_ownership_and_retry(
    frontend: Frontend, state: ActionState
) -> None:
    async with frontend.client() as caller, frontend.client("test-token-b") as other:
        request = {
            "idempotency_key": "test-cancel",
            "action": {"group": "test-group", "name": "alpha"},
            "arguments": {"message": "test-cancel"},
        }
        receipt = ActionRequestView.model_validate(
            (await caller.call_tool("request_action", {"request": request})).structured_content
        )
        if state is not ActionState.DECISION_PENDING:
            await frontend.store.decide(
                receipt.id,
                DecisionInput(
                    verdict=Verdict.DENY if state is ActionState.DENIED else Verdict.ALLOW,
                    expected_version=receipt.version,
                    idempotency_key="test-cancel-decision",
                ),
                OPERATOR,
                provider="test-human",
            )
        if state is ActionState.DISPATCHING:
            assert (
                await frontend.store.claim_execution(
                    receipt.id, executor_id="test-executor", lease_duration=timedelta(seconds=30)
                )
                is not None
            )
        args = {"request_id": str(receipt.id)}
        assert (await other.call_tool("cancel_action_request", args, raise_on_error=False)).is_error
        cancelled = CancellationResult.model_validate(
            (await caller.call_tool("cancel_action_request", args)).structured_content
        )
        repeated = CancellationResult.model_validate(
            (await caller.call_tool("cancel_action_request", args)).structured_content
        )
        if state is ActionState.DISPATCHING:
            assert cancelled.outcome is repeated.outcome is CancellationOutcome.TOO_LATE
            assert cancelled.request.state is ActionState.DISPATCHING
        elif state is ActionState.DENIED:
            assert cancelled.outcome is repeated.outcome is CancellationOutcome.ALREADY_FINISHED
            assert cancelled.request.state is ActionState.DENIED
        else:
            assert cancelled.outcome is CancellationOutcome.CANCELLED
            assert repeated.outcome is CancellationOutcome.ALREADY_CANCELLED
            assert cancelled.request.state is ActionState.CANCELLED
        assert repeated.request == cancelled.request
        assert (
            ActionRequestView.model_validate(
                (await caller.call_tool("request_action", {"request": request})).structured_content
            )
            == cancelled.request
        )


async def test_cancellation_wakes_mcp_receipt_wait(frontend: Frontend, subscription_signals: WaitSignals) -> None:
    async with frontend.client() as client:
        receipt = ActionRequestView.model_validate(
            (
                await client.call_tool(
                    "request_action",
                    {
                        "request": {
                            "idempotency_key": "test-cancel-wake",
                            "action": {"group": "test-group", "name": "alpha"},
                            "arguments": {"message": "test-cancel-wake"},
                        }
                    },
                )
            ).structured_content
        )
        async with asyncio.timeout(10):
            pending = asyncio.create_task(
                client.call_tool("get_action_request", {"request_id": str(receipt.id), "wait_seconds": 30})
            )
            await subscription_signals.registered.wait()
            cancelled = CancellationResult.model_validate(
                (await client.call_tool("cancel_action_request", {"request_id": str(receipt.id)})).structured_content
            )
            assert cancelled.outcome is CancellationOutcome.CANCELLED
            assert ActionRequestView.model_validate((await pending).structured_content) == cancelled.request
            await subscription_signals.released.wait()
        assert not frontend.updates._subscribers


if __name__ == "__main__":
    pytest_bazel.main()
