"""Service-level P0 replay: submit, review, decide, single dispatch, terminal projection."""

from __future__ import annotations

import asyncio
from typing import cast

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from x.agentplane.action_service.api import create_app
from x.agentplane.action_service.auth import Authenticator
from x.agentplane.action_service.db import ActionStore, OutboxRow, make_sessionmaker
from x.agentplane.action_service.models import (
    ActionRequestInput,
    ActionState,
    DecisionInput,
    ExecutionRequest,
    ExecutionResult,
    ExecutionState,
    Principal,
    PrincipalRole,
    Verdict,
)
from x.agentplane.action_service.service import ActionService

CALLER = Principal(issuer="test", subject="caller-a", role=PrincipalRole.CALLER)
OTHER = Principal(issuer="test", subject="caller-b", role=PrincipalRole.CALLER)
OPERATOR = Principal(issuer="test", subject="operator", role=PrincipalRole.OPERATOR)
TOKENS = {"caller-token": CALLER, "other-token": OTHER, "operator-token": OPERATOR}


class FakeAuthenticator:
    async def authenticate(self, token: str) -> Principal | None:
        return TOKENS.get(token)


class CountingExecutor:
    def __init__(self) -> None:
        self.requests: list[ExecutionRequest] = []

    @property
    def capabilities(self) -> frozenset[str]:
        return frozenset({"agentplane:v0.echo"})

    async def execute(self, request: ExecutionRequest) -> ExecutionResult:
        self.requests.append(request)
        return ExecutionResult(
            state=ExecutionState.SUCCEEDED, result={"echo": request.arguments, "credential": "must-redact"}
        )


async def _client(service: ActionService) -> httpx.AsyncClient:
    app = create_app(service, cast(Authenticator, FakeAuthenticator()))
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://actions.test")


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _terminal(client: httpx.AsyncClient, request_id: str, token: str = "caller-token") -> dict:
    for _ in range(100):
        response = await client.get(f"/v1/action-requests/{request_id}", headers=_auth(token))
        response.raise_for_status()
        body = response.json()
        if body["state"] in {"succeeded", "failed", "cancelled", "execution_unknown"}:
            return body
        await asyncio.sleep(0.01)
    raise AssertionError("action request did not reach a terminal state")


async def test_p0_allow_and_deny_path_with_scope_redaction_and_replay_protection(engine: AsyncEngine) -> None:
    executor = CountingExecutor()
    store = ActionStore(make_sessionmaker(engine))
    service = ActionService(store, executor)
    await service.start()
    client = await _client(service)
    try:
        envelope = {
            "idempotency_key": "submit-1",
            "capability": "agentplane:v0.echo",
            "arguments": {"text": "hello", "nested": {"api_key": "secret-value"}},
            "origin": {"thread_ref": "thread-opaque", "authorization": "never-project"},
            "correlation": {"turn_ref": "turn-opaque"},
        }
        assert (await client.post("/v1/action-requests", json=envelope)).status_code == 401
        # Caller identity headers are inert; only the authenticator establishes ownership.
        assert (
            await client.post("/v1/action-requests", json=envelope, headers={"x-caller-principal": CALLER.key})
        ).status_code == 401

        submitted = await client.post("/v1/action-requests", json=envelope, headers=_auth("caller-token"))
        assert submitted.status_code == 202
        pending = submitted.json()
        request_id = pending["id"]
        assert pending["state"] == "decision_pending"
        assert pending["caller_principal"] is None
        assert pending["arguments"]["nested"]["api_key"] == "[redacted]"
        assert pending["origin"]["authorization"] == "[redacted]"

        duplicate = await client.post("/v1/action-requests", json=envelope, headers=_auth("caller-token"))
        assert duplicate.json()["id"] == request_id
        changed = dict(envelope)
        changed["arguments"] = {"text": "changed"}
        assert (
            await client.post("/v1/action-requests", json=changed, headers=_auth("caller-token"))
        ).status_code == 409

        assert (await client.get("/v1/action-requests", headers=_auth("other-token"))).json() == []
        assert (await client.get(f"/v1/action-requests/{request_id}", headers=_auth("other-token"))).status_code == 404
        operator_list = await client.get("/v1/action-requests", headers=_auth("operator-token"))
        assert operator_list.json()[0]["caller_principal"] == CALLER.key
        assert operator_list.json()[0]["arguments"]["nested"]["api_key"] == "[redacted]"

        allowed = await client.post(
            f"/v1/action-requests/{request_id}/decision",
            headers=_auth("operator-token"),
            json={
                "verdict": "allow",
                "expected_version": pending["version"],
                "idempotency_key": "decision-allow-1",
                "private_reason": "operator-only context",
            },
        )
        assert allowed.status_code == 200
        assert allowed.json()["state"] == "allowed"
        assert allowed.json()["decision"]["private_reason"] == "operator-only context"

        terminal = await _terminal(client, request_id)
        assert terminal["state"] == "succeeded"
        assert terminal["execution"]["result"] == {
            "echo": {"text": "hello", "nested": {"api_key": "[redacted]"}},
            "credential": "[redacted]",
        }
        assert terminal["decision"]["private_reason"] is None
        assert terminal["decision"]["private_reason_redacted"] is True
        assert len(executor.requests) == 1
        # Executors receive the durable raw envelope; redaction is a read projection, not data loss.
        assert executor.requests[0].arguments["nested"] == {"api_key": "secret-value"}

        replay = await client.post(
            f"/v1/action-requests/{request_id}/decision",
            headers=_auth("operator-token"),
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
        assert (
            await client.post(
                f"/v1/action-requests/{request_id}/decision",
                headers=_auth("operator-token"),
                json={
                    "verdict": "deny",
                    "expected_version": terminal["version"],
                    "idempotency_key": "decision-deny-conflict",
                },
            )
        ).status_code == 409

        history = await client.get(f"/v1/action-requests/{request_id}/events", headers=_auth("caller-token"))
        assert [event["state"] for event in history.json()] == [
            "decision_pending",
            "allowed",
            "dispatching",
            "running",
            "succeeded",
        ]

        denied_submit = await client.post(
            "/v1/action-requests", headers=_auth("other-token"), json={**envelope, "idempotency_key": "submit-denied"}
        )
        denied_pending = denied_submit.json()
        denied = await client.post(
            f"/v1/action-requests/{denied_pending['id']}/decision",
            headers=_auth("operator-token"),
            json={
                "verdict": "deny",
                "expected_version": denied_pending["version"],
                "idempotency_key": "decision-deny-1",
                "private_reason": "not permitted",
            },
        )
        assert denied.json()["state"] == "denied"
        assert len(executor.requests) == 1
        caller_denied = await client.get(f"/v1/action-requests/{denied_pending['id']}", headers=_auth("other-token"))
        assert caller_denied.json()["decision"]["private_reason"] is None
        assert caller_denied.json()["decision"]["private_reason_redacted"] is True

        async with make_sessionmaker(engine)() as session:
            rows = list(await session.scalars(select(OutboxRow).order_by(OutboxRow.created_at)))
            assert len(rows) == 2
            assert rows[0].payload == {"request_id": request_id, "capability": "agentplane:v0.echo"}
            assert "secret-value" not in str(rows[0].payload)
    finally:
        await client.aclose()
        await service.close()


async def test_restart_resumes_only_provably_pending_dispatch_and_marks_inflight_unknown(engine: AsyncEngine) -> None:
    sessions = make_sessionmaker(engine)
    store = ActionStore(sessions)
    executor = CountingExecutor()

    pending = await store.submit(
        ActionRequestInput(
            idempotency_key="restart-pending", capability="agentplane:v0.echo", arguments={"case": "safe"}
        ),
        CALLER,
        supported_capabilities=executor.capabilities,
    )
    pending_view = pending[0]
    _, should_dispatch = await store.decide(
        pending_view.id,
        DecisionInput(verdict=Verdict.ALLOW, expected_version=pending_view.version, idempotency_key="restart-allow"),
        OPERATOR,
        provider=ActionService.HUMAN_PROVIDER,
    )
    assert should_dispatch is True

    restarted = ActionService(store, executor)
    assert await restarted.start() == 0
    client = await _client(restarted)
    try:
        assert (await _terminal(client, str(pending_view.id)))["state"] == "succeeded"
        assert len(executor.requests) == 1
    finally:
        await client.aclose()
        await restarted.close()

    inflight = await store.submit(
        ActionRequestInput(
            idempotency_key="restart-inflight", capability="agentplane:v0.echo", arguments={"case": "unsafe"}
        ),
        CALLER,
        supported_capabilities=executor.capabilities,
    )
    inflight_view = inflight[0]
    await store.decide(
        inflight_view.id,
        DecisionInput(
            verdict=Verdict.ALLOW, expected_version=inflight_view.version, idempotency_key="restart-inflight-allow"
        ),
        OPERATOR,
        provider=ActionService.HUMAN_PROVIDER,
    )
    assert await store.claim_execution(inflight_view.id) is True
    await store.mark_running(inflight_view.id)

    never_called = CountingExecutor()
    after_crash = ActionService(store, never_called)
    assert await after_crash.start() == 1
    unknown = await store.get(inflight_view.id, CALLER)
    assert unknown.state is ActionState.EXECUTION_UNKNOWN
    assert unknown.execution is not None
    assert unknown.execution.error == {"kind": "process_restarted", "message": "dispatch outcome unknown; not replayed"}
    assert never_called.requests == []
    assert [event.state for event in await store.events(inflight_view.id, CALLER)] == [
        ActionState.DECISION_PENDING,
        ActionState.ALLOWED,
        ActionState.DISPATCHING,
        ActionState.RUNNING,
        ActionState.EXECUTION_UNKNOWN,
    ]
    await after_crash.close()
