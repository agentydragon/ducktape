"""Actual MCP HTTP protocol, destination workload validation, and canonical Action receipts."""

from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import AsyncIterator, Iterator, Mapping
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from datetime import timedelta
from unittest.mock import AsyncMock
from uuid import UUID

import httpx2
import pytest
import pytest_bazel
from fastapi import FastAPI
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport
from fastmcp.exceptions import ToolError
from kubernetes_asyncio import client as k8s_client
from kubernetes_asyncio.client import AuthenticationV1Api, CoreV1Api
from mcp.types import CallToolResult, ContentBlock, ImageContent, TextContent
from more_itertools import one
from sqlalchemy.ext.asyncio import AsyncEngine
from starlette.types import Message, Scope

from agentplane.action_service.api import create_app
from agentplane.action_service.auth import DisabledOperatorAuthenticator, workload_principal
from agentplane.action_service.catalog import (
    ActionCatalog,
    ActionDefinition,
    ActionGroup,
    ActionIdentity,
    ExecutorBinding,
    McpExecutorBinding,
)
from agentplane.action_service.client import WORKLOAD_CREDENTIAL_PLACEHOLDER
from agentplane.action_service.conftest import ScriptedExecutor, lifespan_in_own_task
from agentplane.action_service.db import ActionStore, make_sessionmaker
from agentplane.action_service.mcp_frontend import CancellationView, PolicyField, Receipt, RequestField
from agentplane.action_service.models import (
    ActionRequestInput,
    ActionRequestView,
    ActionState,
    CallerPrincipal,
    CancellationOutcome,
    DecisionInput,
    ExecutionResult,
    ExecutionState,
    Executor,
    OperatorPrincipal,
    Verdict,
    service_account_key,
)
from agentplane.action_service.policies.resources import parse_binding, parse_policy_set
from agentplane.action_service.policy_informer import PolicyIndex
from agentplane.action_service.policy_view import CallerActionPolicyView
from agentplane.action_service.sandbox.binding import SandboxExecutorBinding
from agentplane.action_service.sandbox.models import ExecResult
from agentplane.action_service.service import ActionService
from agentplane.action_service.test_fixtures.callers import in_sync_index
from agentplane.action_service.updates import ActionUpdates
from agentplane.subjects import ServiceAccountRef
from agentplane.workload_auth.principal import (
    POD_NAME_CLAIM,
    POD_UID_CLAIM,
    WorkloadPrincipal,
    WorkloadPrincipalResolver,
)
from mcp_infra.exec.models import Exited

AUDIENCE = "test-action-audience"
NAMESPACE = "test-action-sandboxes"
OPERATOR = OperatorPrincipal(issuer="test", subject="operator")
# Passed as include_fields to reconstruct the full wire model where a test needs every field.
ALL_REQUEST_FIELDS = list(RequestField)
ALL_POLICY_FIELDS = list(PolicyField)


def sandbox(label: str) -> WorkloadPrincipal:
    """One sandbox, running as the ServiceAccount of its own that the app mints per Sandbox."""
    return WorkloadPrincipal(
        namespace=NAMESPACE,
        service_account_name=f"test-runner-{label}",
        service_account_subject=f"system:serviceaccount:{NAMESPACE}:test-runner-{label}",
        pod_name=f"test-pod-{label}",
        pod_uid=f"test-pod-uid-{label}",
    )


def _policy_index() -> PolicyIndex:
    """Workload a bound to `test-reads`, workload b to nothing: what each may read of its own policy."""
    index = in_sync_index()
    for name, spec in (
        ("test-reads", {"autoApproveIf": [{"type": "exact_actions", "actions": {"test-group": ["alpha"]}}]}),
        ("test-other", {"autoApproveIf": [{"type": "exact_actions", "actions": {"test-group": ["beta"]}}]}),
    ):
        policy_set = parse_policy_set(
            {
                "metadata": {
                    "name": name,
                    "namespace": NAMESPACE,
                    "uid": f"uid-{name}",
                    "generation": 1,
                    "resourceVersion": "1",
                },
                "spec": spec,
            }
        )
        index.policy_sets[policy_set.namespaced_name] = policy_set
    for name, account, sets in (
        ("test-a-reads", sandbox("a").service_account_name, ["test-reads", "test-vanished"]),
        ("test-elsewhere", "test-runner-elsewhere", ["test-other"]),
    ):
        binding = parse_binding(
            {
                "metadata": {
                    "name": name,
                    "namespace": NAMESPACE,
                    "uid": f"uid-{name}",
                    "generation": 1,
                    "resourceVersion": "1",
                },
                "spec": {"subject": {"namespace": NAMESPACE, "name": account}, "policySets": sets},
            }
        )
        index.bindings[binding.namespaced_name] = binding
    for admitted in (workload("a"), workload("b")):
        index.service_accounts[service_account_key(admitted)] = admitted
    return index


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", "Accept": "application/json, text/event-stream"}


def workload(label: str) -> ServiceAccountRef:
    """The account one sandbox runs as, which is also what admits it here."""
    return ServiceAccountRef(namespace=NAMESPACE, name=sandbox(label).service_account_name)


class EgressSubstitution(httpx2.AsyncBaseTransport):
    """The runner supplies only a public placeholder; substitution is an external boundary."""

    def __init__(self, app: FastAPI, token: str) -> None:
        self._upstream = httpx2.ASGITransport(app)
        self._token = token

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
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
    tokens: dict[str, WorkloadPrincipal]

    def client(self, token: str = "test-token-a", *, egress: bool = False) -> Client[StreamableHttpTransport]:
        def factory(
            headers: dict[str, str] | None = None,
            timeout: httpx2.Timeout | None = None,
            auth: httpx2.Auth | None = None,
            *,
            follow_redirects: bool = True,
        ) -> httpx2.AsyncClient:
            transport = EgressSubstitution(self.app, token) if egress else httpx2.ASGITransport(self.app)
            return httpx2.AsyncClient(
                transport=transport,
                headers=headers,
                timeout=timeout or httpx2.Timeout(30),
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


def _test_group_catalog() -> ActionCatalog:
    return ActionCatalog(
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


@asynccontextmanager
async def _serve(
    engine: AsyncEngine, db_url: str, catalog: ActionCatalog, executors: Mapping[str, Executor]
) -> AsyncIterator[Frontend]:
    tokens = {f"test-token-{label}": sandbox(label) for label in ("a", "b", "elsewhere")}
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

    authentication.create_token_review = AsyncMock(side_effect=review)
    store = ActionStore(make_sessionmaker(engine))
    policies = _policy_index()
    service = ActionService(store, catalog, executors, policies=policies)
    updates = ActionUpdates(db_url)
    app = create_app(
        service,
        WorkloadPrincipalResolver(
            authentication=authentication, audience=AUDIENCE, allowed_service_account_namespaces=(NAMESPACE,)
        ),
        DisabledOperatorAuthenticator(),
        catalog,
        callers=policies,
        updates=updates,
    )
    try:
        async with lifespan_in_own_task(app):
            yield Frontend(app, store, service, authentication, core, updates, tokens)
    finally:
        await service.close()


@pytest.fixture
async def frontend(engine: AsyncEngine, db_url: str, echo_executor: Executor) -> AsyncIterator[Frontend]:
    async with _serve(engine, db_url, _test_group_catalog(), {"test-group": echo_executor}) as served:
        yield served


# One group per executor kind, since how a result reads to an MCP caller depends on which ran it.
RESULT_GROUPS: dict[str, ExecutorBinding] = {
    "test-mcp": McpExecutorBinding(description="test-mcp-executor"),
    "test-sandbox": SandboxExecutorBinding(
        description="test-sandbox-executor", namespace=NAMESPACE, templates={"test-template"}
    ),
}


@pytest.fixture
async def results_frontend(engine: AsyncEngine, db_url: str, scripted: ScriptedExecutor) -> AsyncIterator[Frontend]:
    catalog = ActionCatalog(
        groups={
            key: ActionGroup(
                title=f"Test {key}",
                description=f"{key}-description",
                executor=binding,
                actions={"act": ActionDefinition(description="test-act", input_schema={"type": "object"})},
            )
            for key, binding in RESULT_GROUPS.items()
        }
    )
    async with _serve(engine, db_url, catalog, dict.fromkeys(RESULT_GROUPS, scripted)) as served:
        yield served


async def _submitted(frontend: Frontend, group: str) -> ActionRequestView:
    """Workload a's request for `group`, which no policy decides, so it waits for the operator."""
    return await frontend.service.submit(
        ActionRequestInput(
            idempotency_key=f"test-{group}",
            title=f"test title for {group}",
            action=ActionIdentity(group=group, name="act"),
            arguments={},
        ),
        CallerPrincipal(account=workload("a")),
    )


async def _decide(frontend: Frontend, request: ActionRequestView, verdict: Verdict, note: str | None = None) -> None:
    await frontend.service.decide(
        request.id,
        DecisionInput(
            verdict=verdict,
            expected_version=request.version,
            idempotency_key=f"test-{request.id}-{verdict}",
            decision_note=note,
        ),
        OPERATOR,
    )


def _text(content: list[ContentBlock]) -> str:
    return one(block.text for block in content if isinstance(block, TextContent))


async def test_compact_catalog_opt_in_pagination_and_small_generic_schema(frontend: Frontend) -> None:
    async with frontend.client(egress=True) as client:
        tools = await client.list_tools()
        assert len(tools) == 8
        cancellation = next(tool for tool in tools if tool.name == "cancel_action_request")
        assert set(cancellation.input_schema["properties"]) == {"request_id", "include_fields"}
        assert cancellation.input_schema["required"] == ["request_id"]
        assert all("args" not in tool.input_schema["properties"] for tool in tools)
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


async def test_receipts_and_policy_view_are_compact_by_default_and_widen_via_include_fields(frontend: Frontend) -> None:
    always_receipt_fields = {"id", "state", "version", "created_at", "updated_at"}
    always_policy_fields = {"subject", "synced", "bindings"}
    async with frontend.client() as client:
        envelope = {
            "idempotency_key": "test-compact-receipt",
            "title": "test title for test-compact-receipt",
            "action": {"group": "test-group", "name": "alpha"},
            "arguments": {"message": "test-compact"},
        }
        compact = (await client.call_tool("request_action", {"request": envelope})).structured_content
        assert compact is not None
        assert set(compact) == always_receipt_fields
        request_id = compact["id"]

        # include_fields is a pure allowlist, not additive to the default: naming only the wide
        # fields you want drops the compact ones, so widening past the default means repeating it.
        widened = (
            await client.call_tool(
                "get_action_request", {"request_id": request_id, "include_fields": ["input", "execution"]}
            )
        ).structured_content
        assert widened is not None
        # execution is requested but genuinely null (still decision_pending): the key survives as
        # null, distinguishing "asked for and absent" from "never asked for" -- see _receipt.
        assert set(widened) == {"input", "execution"}
        assert widened["input"] == {**envelope, "description": None}
        assert widened["execution"] is None

        widened_plus_defaults = (
            await client.call_tool(
                "get_action_request",
                {"request_id": request_id, "include_fields": [*always_receipt_fields, "execution"]},
            )
        ).structured_content
        assert widened_plus_defaults is not None
        assert set(widened_plus_defaults) == always_receipt_fields | {"execution"}

        rejected = await client.call_tool(
            "get_action_request", {"request_id": request_id, "include_fields": ["bogus"]}, raise_on_error=False
        )
        assert rejected.is_error

        cancelled = (await client.call_tool("cancel_action_request", {"request_id": request_id})).structured_content
        assert cancelled is not None
        assert set(cancelled) == {"outcome", "request"}
        assert set(cancelled["request"]) == always_receipt_fields

        widened_cancel = (
            await client.call_tool("cancel_action_request", {"request_id": request_id, "include_fields": ["decision"]})
        ).structured_content
        assert widened_cancel is not None
        assert set(widened_cancel["request"]) == {"decision"}

        policy_compact = (await client.call_tool("get_action_policy")).structured_content
        assert policy_compact is not None
        assert set(policy_compact) == always_policy_fields

        policy_widened = (
            await client.call_tool("get_action_policy", {"include_fields": ["auto_approve_if"]})
        ).structured_content
        assert policy_widened is not None
        assert set(policy_widened) == {"auto_approve_if"}
        assert policy_widened["auto_approve_if"] != []


async def test_submission_wait_receipts_events_and_owner_scope(frontend: Frontend) -> None:
    async with frontend.client() as caller, frontend.client("test-token-b") as other:
        key = "test-submit"
        envelope = {
            "idempotency_key": key,
            "title": "test title for test-submit",
            "action": {"group": "test-group", "name": "alpha"},
            "arguments": {"message": "test-message"},
        }
        result = await caller.call_tool("request_action", {"request": envelope, "include_fields": ALL_REQUEST_FIELDS})
        receipt = Receipt.model_validate(result.structured_content)
        assert receipt.state is ActionState.DECISION_PENDING
        assert receipt.id is not None
        request_id = receipt.id
        with pytest.raises(ToolError, match="idempotency key already used"):
            await caller.call_tool("request_action", {"request": envelope})
        by_key = {"idempotency_key": key}
        assert (
            await caller.call_tool("get_action_request", {**by_key, "include_fields": ALL_REQUEST_FIELDS})
        ).structured_content == result.structured_content
        for args in ({}, {"request_id": str(request_id), **by_key}):
            with pytest.raises(ToolError, match="exactly one of request_id or idempotency_key"):
                await caller.call_tool("get_action_request", args)
        for name, args in (
            ("get_action_request", {"request_id": str(request_id)}),
            ("get_action_request", by_key),
            ("get_action_result", {"request_id": str(request_id)}),
            ("get_action_result", by_key),
            ("list_action_request_events", {"request_id": str(request_id)}),
        ):
            denied = await other.call_tool(name, args, raise_on_error=False)
            assert denied.is_error
        task = asyncio.create_task(
            caller.call_tool(
                "get_action_request", {**by_key, "wait": {"wait_seconds": 10}, "include_fields": ALL_REQUEST_FIELDS}
            )
        )
        # A commit before or after subscription must both be observed, without polling.
        await frontend.store.decide(
            request_id,
            DecisionInput(verdict=Verdict.DENY, expected_version=1, idempotency_key="test-deny"),
            OPERATOR,
            provider="test-human",
        )
        assert Receipt.model_validate((await task).structured_content).state is ActionState.DENIED
        first = await caller.call_tool("list_action_request_events", {"request_id": str(request_id), "limit": 1})
        assert first.structured_content is not None
        assert first.structured_content["next_after_sequence"] == 1
        second = await caller.call_tool(
            "list_action_request_events", {"request_id": str(request_id), "after_sequence": 1}
        )
        assert second.structured_content is not None
        assert second.structured_content["events"][0]["state"] == "denied"
        assert "next_after_sequence" not in second.structured_content
        assert (
            await frontend.store.get(request_id, workload_principal(frontend.tokens["test-token-a"]))
        ).id == request_id


async def test_an_unlabelled_account_is_refused_at_the_transport_despite_a_binding(frontend: Frontend) -> None:
    """A binding is what a subject may do; the caller label is whether it may ask at all. The
    `test-elsewhere` account has the former and not the latter, so its token proves a Pod and still
    opens no MCP session -- the same refusal an unknown bearer gets."""
    async with httpx2.AsyncClient(transport=httpx2.ASGITransport(frontend.app), base_url="http://actions.test") as http:
        initialize = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
        refused = await http.post("/mcp", headers=_bearer("test-token-elsewhere"), json=initialize)
        admitted = await http.post("/mcp", headers=_bearer("test-token-b"), json=initialize)

    assert refused.status_code == 401
    assert admitted.status_code != 401, "the labelled account with no binding still opens a session"


async def test_a_caller_reads_the_effective_policy_of_itself_or_a_named_target(frontend: Frontend) -> None:
    """The tool answers for the authenticated Sandbox unless a target is named: its bindings and the
    sets that resolved (a name nothing answers to is simply absent), and another Sandbox's binding
    in the same namespace appears only when that Sandbox is the target. The HTTP route is the
    caller's own view. Nothing is submitted by reading."""
    async with frontend.client(egress=True) as caller, frontend.client("test-token-b") as other:
        own = CallerActionPolicyView.model_validate(
            (await caller.call_tool("get_action_policy", {"include_fields": ALL_POLICY_FIELDS})).structured_content
        )
        assert isinstance(own.subject, ServiceAccountRef)
        assert own.subject == workload_principal(frontend.tokens["test-token-a"]).account
        assert own.synced is True
        assert [(binding.name, binding.policy_sets) for binding in own.bindings] == [("test-a-reads", ["test-reads"])]
        assert [(p.binding, p.policy_set, p.index, p.policy.actions) for p in own.auto_approve_if] == [
            ("test-a-reads", "test-reads", 0, {"test-group": ["alpha"]})
        ]
        assert "test-vanished" not in str(own)
        assert "test-elsewhere" not in str(own)
        by_name = await caller.call_tool("get_action_policy", {"target": "self", "include_fields": ALL_POLICY_FIELDS})
        assert CallerActionPolicyView.model_validate(by_name.structured_content) == own
        nothing = CallerActionPolicyView.model_validate(
            (await other.call_tool("get_action_policy", {"include_fields": ALL_POLICY_FIELDS})).structured_content
        )
        assert nothing.subject == workload_principal(frontend.tokens["test-token-b"]).account
        assert (nothing.synced, nothing.bindings, nothing.auto_approve_if) == (True, [], [])
        # A named target gets the same view its own caller would; one the service does not watch has nothing.
        about_a = await other.call_tool(
            "get_action_policy",
            {"target": {"service_account": own.subject.model_dump()}, "include_fields": ALL_POLICY_FIELDS},
        )
        assert CallerActionPolicyView.model_validate(about_a.structured_content) == own
        elsewhere = await caller.call_tool(
            "get_action_policy",
            {
                "target": {"service_account": {"namespace": NAMESPACE, "name": "test-runner-elsewhere"}},
                "include_fields": ALL_POLICY_FIELDS,
            },
        )
        assert [b.name for b in CallerActionPolicyView.model_validate(elsewhere.structured_content).bindings] == [
            "test-elsewhere"
        ]
        unwatched = await caller.call_tool(
            "get_action_policy",
            {
                "target": {"service_account": {"namespace": "test-unwatched", "name": "nobody"}},
                "include_fields": ALL_POLICY_FIELDS,
            },
        )
        assert CallerActionPolicyView.model_validate(unwatched.structured_content).bindings == []
    assert await frontend.store.list_requests(OPERATOR) == []
    async with httpx2.AsyncClient(transport=httpx2.ASGITransport(frontend.app), base_url="http://actions.test") as http:
        over_http = await http.get("/v1/action-policy", headers={"Authorization": "Bearer test-token-a"})
        assert over_http.status_code == 200, over_http.text
        assert CallerActionPolicyView.model_validate(over_http.json()) == own
        assert (await http.get("/v1/action-policy")).status_code == 401


@pytest.mark.parametrize("authorization", [None, "Bearer test-operator", f"Bearer {WORKLOAD_CREDENTIAL_PLACEHOLDER}"])
async def test_transport_requires_real_workload_bearer(frontend: Frontend, authorization: str | None) -> None:
    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(frontend.app), base_url="http://actions.test"
    ) as client:
        headers = {"Authorization": authorization} if authorization is not None else {}
        for method in ("POST", "DELETE"):
            response = await client.request(method, "/mcp", headers=headers)
            assert response.status_code == 401
            # RFC 6750 §3.1: an error attribute only once credentials were presented and refused.
            assert ('error="invalid_token"' in response.headers["www-authenticate"]) == (authorization is not None)


@pytest.mark.parametrize(
    "body",
    [
        {
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": "test-client", "version": "0"},
            },
        },
        {"method": "tools/list"},
    ],
    ids=["initialize", "tools/list"],
)
async def test_protocol_setup_needs_a_bearer_at_the_transport(frontend: Frontend, body: dict[str, object]) -> None:
    async with httpx2.AsyncClient(transport=httpx2.ASGITransport(frontend.app), base_url="http://actions.test") as http:
        response = await http.post(
            "/mcp",
            headers={"Accept": "application/json, text/event-stream", "MCP-Protocol-Version": "2025-11-25"},
            json={"jsonrpc": "2.0", "id": 1, **body},
        )
        assert response.status_code == 401
        assert response.headers["www-authenticate"] == "Bearer"


async def test_tools_act_as_the_identity_the_transport_verified(frontend: Frontend) -> None:
    request_ids: dict[str, UUID] = {}
    for token in ("test-token-a", "test-token-b"):
        async with frontend.client(token) as client:
            result = await client.call_tool(
                "request_action",
                {
                    "request": {
                        "idempotency_key": f"test-identity-{token}",
                        "title": "test title for test-identity",
                        "action": {"group": "test-group", "name": "alpha"},
                        "arguments": {"message": "test-identity"},
                    }
                },
            )
            assert result.structured_content is not None
            request_ids[token] = UUID(result.structured_content["id"])
    for token, request_id in request_ids.items():
        stored = await frontend.store.get(request_id, OPERATOR)
        assert stored.caller == workload_principal(frontend.tokens[token]).account
        assert stored.external_grant is None


async def test_rejects_a_revoked_bearer_duplicate_auth_and_forged_caller_fields(frontend: Frontend) -> None:
    async with frontend.client() as client:
        invalid = await client.call_tool(
            "request_action",
            {
                "request": {
                    "idempotency_key": "test-forgery",
                    "title": "test title for test-forgery",
                    "action": {"group": "test-group", "name": "alpha"},
                    "arguments": {"message": "test"},
                    "caller_principal": "test-other",
                }
            },
            raise_on_error=False,
        )
        assert invalid.is_error
        assert await frontend.store.list_requests(OPERATOR) == []
        # A bearer is revoked at the TokenReview now, which is the only thing consulted.
        del frontend.tokens["test-token-a"]
    async with httpx2.AsyncClient(transport=httpx2.ASGITransport(frontend.app), base_url="http://actions.test") as http:
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
    async with httpx2.AsyncClient(transport=httpx2.ASGITransport(frontend.app), base_url="http://127.0.0.1") as http:
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
            "title": "test title for test-bounded-submit",
            "action": {"group": "test-group", "name": "alpha"},
            "arguments": {"message": "test-wait"},
        }
        submitted = (
            await client.call_tool("request_action", {"request": request, "wait": {"wait_seconds": 0.001}})
        ).structured_content
        assert submitted is not None
        assert submitted["state"] == ActionState.DECISION_PENDING
        assert not frontend.updates._subscribers
        for seconds in (-1, 31):
            invalid = await client.call_tool(
                "request_action",
                {"request": {**request, "idempotency_key": "test-invalid"}, "wait": {"wait_seconds": seconds}},
                raise_on_error=False,
            )
            assert invalid.is_error
        assert len(await frontend.store.list_requests(OPERATOR)) == 1


async def test_allowed_action_executes_and_its_receipt_reports_the_execution_without_its_result(
    frontend: Frontend,
) -> None:
    async with frontend.client() as client:
        result = await client.call_tool(
            "request_action",
            {
                "request": {
                    "idempotency_key": "test-execute",
                    "title": "test title for test-execute",
                    "action": {"group": "test-group", "name": "alpha"},
                    "arguments": {"message": "test-result"},
                }
            },
        )
        assert result.structured_content is not None
        request_id, version = result.structured_content["id"], result.structured_content["version"]
        await frontend.service.decide(
            UUID(request_id),
            DecisionInput(verdict=Verdict.ALLOW, expected_version=version, idempotency_key="test-allow"),
            OPERATOR,
        )
        finished = (
            await client.call_tool(
                "get_action_request",
                {"request_id": request_id, "wait": {"wait_seconds": 10}, "include_fields": ["state", "execution"]},
            )
        ).structured_content
        assert finished is not None
        assert finished["state"] == ActionState.SUCCEEDED
        assert finished["execution"]["state"] == ExecutionState.SUCCEEDED
        # The result is get_action_result's to return, as the tool answered; a receipt never nests it.
        assert "result" not in finished["execution"]


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
                                "title": "test title for test-disconnect",
                                "action": {"group": "test-group", "name": "alpha"},
                                "arguments": {"message": "test-disconnect"},
                            },
                            "wait": {"wait_seconds": 30},
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
        key = "test-cancel"
        request = {
            "idempotency_key": key,
            "title": "test title for test-cancel",
            "action": {"group": "test-group", "name": "alpha"},
            "arguments": {"message": "test-cancel"},
        }
        submitted = (await caller.call_tool("request_action", {"request": request})).structured_content
        assert submitted is not None
        request_id, version = UUID(submitted["id"]), submitted["version"]
        if state is not ActionState.DECISION_PENDING:
            await frontend.store.decide(
                request_id,
                DecisionInput(
                    verdict=Verdict.DENY if state is ActionState.DENIED else Verdict.ALLOW,
                    expected_version=version,
                    idempotency_key="test-cancel-decision",
                ),
                OPERATOR,
                provider="test-human",
            )
        if state is ActionState.DISPATCHING:
            assert (
                await frontend.store.claim_execution(
                    request_id, executor_id="test-executor", lease_duration=timedelta(seconds=30)
                )
                is not None
            )
        args = {"request_id": str(request_id), "include_fields": ALL_REQUEST_FIELDS}
        assert (await other.call_tool("cancel_action_request", args, raise_on_error=False)).is_error
        cancelled = CancellationView.model_validate(
            (await caller.call_tool("cancel_action_request", args)).structured_content
        )
        repeated = CancellationView.model_validate(
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
        with pytest.raises(ToolError, match="idempotency key already used"):
            await caller.call_tool("request_action", {"request": request})
        by_key = {"idempotency_key": key, "include_fields": ALL_REQUEST_FIELDS}
        recovered = (await caller.call_tool("get_action_request", by_key)).structured_content
        assert Receipt.model_validate(recovered) == cancelled.request


async def test_cancellation_wakes_mcp_receipt_wait(frontend: Frontend, subscription_signals: WaitSignals) -> None:
    async with frontend.client() as client:
        submitted = (
            await client.call_tool(
                "request_action",
                {
                    "request": {
                        "idempotency_key": "test-cancel-wake",
                        "title": "test title for test-cancel-wake",
                        "action": {"group": "test-group", "name": "alpha"},
                        "arguments": {"message": "test-cancel-wake"},
                    }
                },
            )
        ).structured_content
        assert submitted is not None
        request_id = submitted["id"]
        async with asyncio.timeout(10):
            pending = asyncio.create_task(
                client.call_tool(
                    "get_action_request",
                    {"request_id": request_id, "wait": {"wait_seconds": 30}, "include_fields": ALL_REQUEST_FIELDS},
                )
            )
            await subscription_signals.registered.wait()
            cancelled = CancellationView.model_validate(
                (
                    await client.call_tool(
                        "cancel_action_request", {"request_id": request_id, "include_fields": ALL_REQUEST_FIELDS}
                    )
                ).structured_content
            )
            assert cancelled.outcome is CancellationOutcome.CANCELLED
            assert Receipt.model_validate((await pending).structured_content) == cancelled.request
            await subscription_signals.released.wait()
        assert not frontend.updates._subscribers


async def test_action_result_relays_the_mcp_tools_own_answer(
    results_frontend: Frontend, scripted: ScriptedExecutor
) -> None:
    image = base64.b64encode(b"test-image-bytes").decode()
    answer = CallToolResult(
        content=[
            TextContent(type="text", text="test caption"),
            ImageContent(type="image", data=image, mime_type="image/png"),
        ],
        structured_content={"width": 1},
        is_error=True,
    )
    scripted.results[ActionIdentity(group="test-mcp", name="act")] = ExecutionResult(
        state=ExecutionState.SUCCEEDED, result=answer.model_dump(mode="json", by_alias=True, exclude_none=True)
    )
    request = await _submitted(results_frontend, "test-mcp")
    await _decide(results_frontend, request, Verdict.ALLOW)
    async with results_frontend.client() as client:
        result = await client.call_tool(
            "get_action_result", {"request_id": str(request.id), "wait_seconds": 10}, raise_on_error=False
        )
    # The tool's own error answer stays an error, beside the image the receipt could only carry as text.
    assert result.content == answer.content
    assert result.structured_content == {"width": 1}
    assert result.is_error


async def test_action_result_presents_a_sandbox_result_as_a_returned_model(
    results_frontend: Frontend, scripted: ScriptedExecutor
) -> None:
    ran = ExecResult(exit=Exited(exit_code=1), stdout="", stderr="test-missing-file", duration_seconds=0.5)
    scripted.results[ActionIdentity(group="test-sandbox", name="act")] = ExecutionResult(
        state=ExecutionState.SUCCEEDED, result=ran.model_dump(mode="json")
    )
    request = await _submitted(results_frontend, "test-sandbox")
    await _decide(results_frontend, request, Verdict.ALLOW)
    async with results_frontend.client() as client:
        result = await client.call_tool("get_action_result", {"request_id": str(request.id), "wait_seconds": 10})
    # A nonzero exit is the command's answer, not a failed call.
    assert not result.is_error
    assert ExecResult.model_validate(result.structured_content) == ran
    assert ExecResult.model_validate_json(_text(result.content)) == ran


async def test_action_result_says_what_it_waits_on_and_why_nothing_ran(results_frontend: Frontend) -> None:
    request = await _submitted(results_frontend, "test-mcp")
    async with results_frontend.client() as client:
        pending = await client.call_tool("get_action_result", {"idempotency_key": request.idempotency_key})
        await _decide(results_frontend, request, Verdict.DENY, note="test-denial-note")
        denied = await client.call_tool(
            "get_action_result", {"request_id": str(request.id), "wait_seconds": 10}, raise_on_error=False
        )
    assert not pending.is_error
    assert pending.structured_content == {"request_id": str(request.id), "state": ActionState.DECISION_PENDING}
    assert denied.is_error
    assert "test-denial-note" in _text(denied.content)


async def test_action_result_of_a_failed_execution_carries_its_reason(
    results_frontend: Frontend, scripted: ScriptedExecutor
) -> None:
    scripted.results[ActionIdentity(group="test-sandbox", name="act")] = ExecutionResult(
        state=ExecutionState.FAILED, error={"kind": "sandbox_unavailable", "message": "test box is not ready"}
    )
    request = await _submitted(results_frontend, "test-sandbox")
    await _decide(results_frontend, request, Verdict.ALLOW)
    async with results_frontend.client() as client:
        result = await client.call_tool(
            "get_action_result", {"request_id": str(request.id), "wait_seconds": 10}, raise_on_error=False
        )
    assert result.is_error
    assert "sandbox_unavailable" in _text(result.content)


if __name__ == "__main__":
    pytest_bazel.main()
