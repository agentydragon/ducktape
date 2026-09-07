"""P0 vertical seam: Sandbox ownership, separate review, decisions, dispatch, and recovery."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any, cast

import httpx
import pytest_bazel
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from x.agentplane.action_service.api import create_app
from x.agentplane.action_service.auth import OperatorAuthenticator, workload_principal
from x.agentplane.action_service.catalog import ActionCatalog, ActionDefinition, ActionGroup, ExecutorBinding
from x.agentplane.action_service.db import ActionStore, ExecutionRow, OutboxRow, make_sessionmaker
from x.agentplane.action_service.models import (
    ActionRequestInput,
    ActionState,
    DecisionInput,
    ExecutionLease,
    ExecutionRequest,
    ExecutionResult,
    ExecutionState,
    Principal,
    PrincipalRole,
    Verdict,
)
from x.agentplane.action_service.service import ActionService
from x.agentplane.sandbox_auth.http import SandboxPrincipalAuthenticator
from x.agentplane.sandbox_auth.principal import SandboxPrincipal

NAMESPACE = "agentplane-staging"
SERVICE_ACCOUNT_SUBJECT = f"system:serviceaccount:{NAMESPACE}:agentplane-runner"
SANDBOX_A = SandboxPrincipal(
    namespace=NAMESPACE,
    service_account_name="agentplane-runner",
    service_account_subject=SERVICE_ACCOUNT_SUBJECT,
    pod_name="sandbox-a-pod",
    pod_uid="pod-a-uid",
    sandbox_name="sandbox-a",
    sandbox_uid="sandbox-a-uid",
)
SANDBOX_B = SandboxPrincipal(
    namespace=NAMESPACE,
    service_account_name="agentplane-runner",
    service_account_subject=SERVICE_ACCOUNT_SUBJECT,
    pod_name="sandbox-b-pod",
    pod_uid="pod-b-uid",
    sandbox_name="sandbox-b",
    sandbox_uid="sandbox-b-uid",
)
CALLER_A = workload_principal(SANDBOX_A)
CALLER_B = workload_principal(SANDBOX_B)
OPERATOR = Principal(issuer="test-bff", subject="operator", role=PrincipalRole.OPERATOR)
WORKLOAD_TOKENS = {"workload-a": SANDBOX_A, "workload-b": SANDBOX_B}


class FakeSandboxAuthenticator:
    async def __call__(self, request: Any) -> SandboxPrincipal:
        value = request.headers.get("authorization", "")
        if not value.startswith("Bearer ") or value.removeprefix("Bearer ") not in WORKLOAD_TOKENS:
            raise HTTPException(401, "invalid workload bearer")
        return WORKLOAD_TOKENS[value.removeprefix("Bearer ")]


class FakeOperatorAuthenticator:
    async def authenticate(self, token: str) -> Principal | None:
        return OPERATOR if token == "operator-bff" else None


class CountingExecutor:
    def __init__(self) -> None:
        self.requests: list[ExecutionRequest] = []

    @property
    def capabilities(self) -> frozenset[str]:
        return frozenset({"agentplane:v0.echo"})

    async def execute(self, request: ExecutionRequest, lease: ExecutionLease) -> ExecutionResult:
        self.requests.append(request)
        return ExecutionResult(
            state=ExecutionState.SUCCEEDED,
            result={"echo": request.arguments, "credential": "provider-material-must-redact"},
        )


class LeakyFailingExecutor(CountingExecutor):
    async def execute(self, request: ExecutionRequest, lease: ExecutionLease) -> ExecutionResult:
        self.requests.append(request)
        raise RuntimeError("provider rejected Authorization: Bearer provider-token-must-not-escape")


async def _client(service: ActionService, *, catalog: ActionCatalog | None = None) -> httpx.AsyncClient:
    app = create_app(
        service,
        cast(SandboxPrincipalAuthenticator, FakeSandboxAuthenticator()),
        cast(OperatorAuthenticator, FakeOperatorAuthenticator()),
        catalog or ActionCatalog(),
    )
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://actions.test")


def _workload(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _operator() -> dict[str, str]:
    return {"Authorization": "Bearer operator-bff"}


def _operator_path(request_id: str, suffix: str = "") -> str:
    return f"/v1/operator/action-requests/{request_id}{suffix}"


async def _terminal(client: httpx.AsyncClient, request_id: str, token: str = "workload-a") -> dict[str, Any]:
    for _ in range(100):
        response = await client.get(f"/v1/action-requests/{request_id}", headers=_workload(token))
        response.raise_for_status()
        body = cast(dict[str, Any], response.json())
        if body["state"] in {"succeeded", "failed", "cancelled", "execution_unknown"}:
            return body
        await asyncio.sleep(0.01)
    raise AssertionError("action request did not reach a terminal state")


async def test_p0_allow_deny_scope_forgery_redaction_and_single_execution(engine: AsyncEngine) -> None:
    executor = CountingExecutor()
    store = ActionStore(make_sessionmaker(engine))
    service = ActionService(store, executor)
    await service.start()
    client = await _client(service)
    try:
        envelope = {
            "idempotency_key": "submit-1",
            "capability": "agentplane:v0.echo",
            "arguments": {"text": "hello", "nested": {"api_key": "provider-material"}},
            # Every identity-like value here is deliberately forged, accepted only as untrusted
            # provenance, and must not affect the caller derived from SandboxPrincipal.
            "origin": {
                "owner": CALLER_B.key,
                "sandbox_id": SANDBOX_B.sandbox_uid,
                "thread_id": "forged-thread",
                "agent_id": "forged-agent",
                "caller_role": "operator",
                "authorization": "must-not-project",
            },
            "correlation": {"turn_ref": "turn-opaque"},
        }
        assert (await client.post("/v1/action-requests", json=envelope)).status_code == 401
        assert (
            await client.post(
                "/v1/action-requests",
                json=envelope,
                headers={"x-sandbox-uid": SANDBOX_B.sandbox_uid, "x-caller-principal": CALLER_B.key},
            )
        ).status_code == 401
        assert (await client.post("/v1/action-requests", json=envelope, headers=_operator())).status_code == 401, (
            "operator auth is not consulted on the workload surface"
        )

        for forged_top_level in ("owner", "sandbox_id", "thread_id", "agent_id", "caller_role"):
            response = await client.post(
                "/v1/action-requests", json={**envelope, forged_top_level: "forged"}, headers=_workload("workload-a")
            )
            assert response.status_code == 422

        submitted, concurrent_duplicate = await asyncio.gather(
            client.post("/v1/action-requests", json=envelope, headers=_workload("workload-a")),
            client.post("/v1/action-requests", json=envelope, headers=_workload("workload-a")),
        )
        assert submitted.status_code == concurrent_duplicate.status_code == 202
        assert concurrent_duplicate.json()["id"] == submitted.json()["id"]
        pending = submitted.json()
        request_id = pending["id"]
        assert pending["state"] == "decision_pending"
        assert pending["caller_principal"] is None
        assert pending["arguments"]["nested"]["api_key"] == "[redacted]"
        assert pending["origin"]["authorization"] == "[redacted]"

        # Both Pods use the same ServiceAccount; live Sandbox identity, not subject, owns the row.
        assert SANDBOX_A.service_account_subject == SANDBOX_B.service_account_subject
        assert (await client.get("/v1/action-requests", headers=_workload("workload-b"))).json() == []
        assert (
            await client.get(f"/v1/action-requests/{request_id}", headers=_workload("workload-b"))
        ).status_code == 404
        operator_list = await client.get("/v1/operator/action-requests", headers=_operator())
        assert operator_list.status_code == 200
        assert operator_list.json()[0]["caller_principal"] == CALLER_A.key
        assert operator_list.json()[0]["origin"]["owner"] == CALLER_B.key, "forgery remains inert provenance"

        stale = await client.post(
            _operator_path(request_id, "/decision"),
            headers=_operator(),
            json={"verdict": "allow", "expected_version": 99, "idempotency_key": "stale"},
        )
        assert stale.status_code == 409

        decision = {
            "verdict": "allow",
            "expected_version": pending["version"],
            "idempotency_key": "decision-allow-1",
            "private_reason": "private reviewer context",
        }
        allowed, duplicate_allow = await asyncio.gather(
            client.post(_operator_path(request_id, "/decision"), headers=_operator(), json=decision),
            client.post(_operator_path(request_id, "/decision"), headers=_operator(), json=decision),
        )
        assert allowed.status_code == duplicate_allow.status_code == 200
        assert allowed.json()["decision"]["private_reason"] == "private reviewer context"

        terminal = await _terminal(client, request_id)
        assert terminal["state"] == "succeeded"
        assert terminal["execution"]["result"] == {
            "echo": {"text": "hello", "nested": {"api_key": "[redacted]"}},
            "credential": "[redacted]",
        }
        assert terminal["decision"]["private_reason"] is None
        assert terminal["decision"]["private_reason_redacted"] is True
        assert len(executor.requests) == 1
        assert executor.requests[0].arguments["nested"] == {"api_key": "provider-material"}

        replay = await client.post(
            _operator_path(request_id, "/decision"),
            headers=_operator(),
            json={
                "verdict": "allow",
                "expected_version": 1,
                "idempotency_key": "decision-allow-1",
                "private_reason": "ignored on replay",
            },
        )
        assert replay.status_code == 200
        assert replay.json()["state"] == "succeeded"
        assert len(executor.requests) == 1

        history = await client.get(f"/v1/action-requests/{request_id}/events", headers=_workload("workload-a"))
        assert [event["state"] for event in history.json()] == [
            "decision_pending",
            "allowed",
            "dispatching",
            "running",
            "succeeded",
        ]

        denied_submit = await client.post(
            "/v1/action-requests",
            headers=_workload("workload-b"),
            json={**envelope, "idempotency_key": "submit-denied"},
        )
        denied_pending = denied_submit.json()
        denied = await client.post(
            _operator_path(denied_pending["id"], "/decision"),
            headers=_operator(),
            json={
                "verdict": "deny",
                "expected_version": denied_pending["version"],
                "idempotency_key": "decision-deny-1",
                "private_reason": "not permitted",
            },
        )
        assert denied.json()["state"] == "denied"
        assert len(executor.requests) == 1

        async with make_sessionmaker(engine)() as session:
            outbox = list(await session.scalars(select(OutboxRow).order_by(OutboxRow.created_at)))
            executions = list(await session.scalars(select(ExecutionRow)))
            assert len(outbox) == 2
            assert outbox[0].payload == {"request_id": request_id, "capability": "agentplane:v0.echo"}
            assert "provider-material" not in str([row.payload for row in outbox])
            assert len(executions) == 1
    finally:
        await client.aclose()
        await service.close()


async def test_executor_exception_material_is_not_logged_projected_or_retried(engine: AsyncEngine, caplog: Any) -> None:
    executor = LeakyFailingExecutor()
    store = ActionStore(make_sessionmaker(engine))
    service = ActionService(store, executor)
    await service.start()
    client = await _client(service)
    try:
        pending = (
            await client.post(
                "/v1/action-requests",
                headers=_workload("workload-a"),
                json={
                    "idempotency_key": "leaky-failure",
                    "capability": "agentplane:v0.echo",
                    "arguments": {"safe": True},
                },
            )
        ).json()
        response = await client.post(
            _operator_path(pending["id"], "/decision"),
            headers=_operator(),
            json={"verdict": "allow", "expected_version": pending["version"], "idempotency_key": "allow-leaky-failure"},
        )
        assert response.status_code == 200
        failed = await _terminal(client, pending["id"])
        assert failed["state"] == "failed"
        assert failed["execution"]["error"] == {
            "kind": "RuntimeError",
            "message": "executor failed; see credential-safe adapter metrics",
        }
        assert len(executor.requests) == 1

        restarted = ActionService(store, executor)
        await restarted.start()
        await asyncio.sleep(0)
        assert len(executor.requests) == 1
        await restarted.close()

        rendered = failed.__str__() + "\n" + "\n".join(record.getMessage() for record in caplog.records)
        assert "provider-token-must-not-escape" not in rendered
    finally:
        await client.aclose()
        await service.close()


async def test_restart_resumes_only_pending_dispatch_and_leaves_inflight_work_to_the_lease_sweep(
    engine: AsyncEngine,
) -> None:
    """A restart never assumes in-flight work died with the old process (see executor_liveness.md):
    pending dispatches resume immediately, but dispatching/running work is untouched until its own
    lease bound expires — whether that is because the old process crashed or a separate worker did.
    """
    sessions = make_sessionmaker(engine)
    store = ActionStore(sessions)
    executor = CountingExecutor()

    pending_view, _ = await store.submit(
        ActionRequestInput(
            idempotency_key="restart-pending", capability="agentplane:v0.echo", arguments={"case": "safe"}
        ),
        CALLER_A,
        supported_capabilities=executor.capabilities,
    )
    _, should_dispatch = await store.decide(
        pending_view.id,
        DecisionInput(verdict=Verdict.ALLOW, expected_version=pending_view.version, idempotency_key="restart-allow"),
        OPERATOR,
        provider=ActionService.HUMAN_PROVIDER,
    )
    assert should_dispatch is True

    restarted = ActionService(store, executor)
    await restarted.start()
    client = await _client(restarted)
    try:
        assert (await _terminal(client, str(pending_view.id)))["state"] == "succeeded"
        assert len(executor.requests) == 1
    finally:
        await client.aclose()
        await restarted.close()

    inflight_view, _ = await store.submit(
        ActionRequestInput(
            idempotency_key="restart-inflight", capability="agentplane:v0.echo", arguments={"case": "unsafe"}
        ),
        CALLER_A,
        supported_capabilities=executor.capabilities,
    )
    await store.decide(
        inflight_view.id,
        DecisionInput(
            verdict=Verdict.ALLOW, expected_version=inflight_view.version, idempotency_key="restart-inflight-allow"
        ),
        OPERATOR,
        provider=ActionService.HUMAN_PROVIDER,
    )
    claim = await store.claim_execution(
        inflight_view.id, executor_id="crashed-executor", lease_duration=timedelta(minutes=5)
    )
    assert claim is not None
    await store.mark_running(inflight_view.id)

    never_called = CountingExecutor()
    after_crash = ActionService(store, never_called)
    await after_crash.start()
    await asyncio.sleep(0)
    try:
        # The lease from before the crash is still comfortably unexpired, so the new process
        # must not assume the old one's work is dead.
        still_running = await store.get(inflight_view.id, CALLER_A)
        assert still_running.state is ActionState.RUNNING
        assert never_called.requests == []
    finally:
        await after_crash.close()


async def test_configured_catalog_is_discoverable_and_unknown_lookups_fail_clearly(engine: AsyncEngine) -> None:
    catalog = ActionCatalog(
        groups={
            "github": ActionGroup(
                title="GitHub",
                description="Read access to public GitHub repositories.",
                executor=ExecutorBinding(
                    kind="mcp",
                    description="Connected as Rai's GitHub account.",
                    config={"account_secret_ref": "github-mcp-account"},
                ),
                actions={
                    "get_file": ActionDefinition(
                        description="Read one file's contents from a public repository.",
                        input_schema={"type": "object", "properties": {"path": {"type": "string"}}},
                    )
                },
            )
        }
    )
    store = ActionStore(make_sessionmaker(engine))
    service = ActionService(store, CountingExecutor())
    await service.start()
    client = await _client(service, catalog=catalog)
    try:
        groups = await client.get("/v1/action-groups", headers=_workload("workload-a"))
        assert groups.status_code == 200
        assert groups.json() == [
            {
                "key": "github",
                "title": "GitHub",
                "description": "Read access to public GitHub repositories.",
                "executor_kind": "mcp",
                "executor_description": "Connected as Rai's GitHub account.",
                "available": True,
                "actions": [
                    {
                        "group": "github",
                        "name": "get_file",
                        "id": "github.get_file",
                        "description": "Read one file's contents from a public repository.",
                        "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}},
                    }
                ],
            }
        ]
        assert "github-mcp-account" not in groups.text

        unauthenticated = await client.get("/v1/action-groups")
        assert unauthenticated.status_code == 401

        action = await client.get("/v1/action-groups/github/actions/get_file", headers=_workload("workload-a"))
        assert action.status_code == 200
        assert action.json()["id"] == "github.get_file"

        missing_group = await client.get(
            "/v1/action-groups/does-not-exist/actions/get_file", headers=_workload("workload-a")
        )
        assert missing_group.status_code == 404

        missing_action = await client.get(
            "/v1/action-groups/github/actions/does-not-exist", headers=_workload("workload-a")
        )
        assert missing_action.status_code == 404
    finally:
        await client.aclose()
        await service.close()


if __name__ == "__main__":
    pytest_bazel.main()
