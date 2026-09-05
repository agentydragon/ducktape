"""The authenticated ActionRequest API: caller ownership and operator review/Decision scope."""

from __future__ import annotations

import asyncio

import httpx
import pytest_bazel
from fastapi import Request

from x.agentplane.app.actions import ActionHub, EchoExecutor
from x.agentplane.app.api import Provider, create_app
from x.agentplane.app.bridge import RunnerBridge
from x.agentplane.app.decisions import DecisionsClient
from x.agentplane.app.egress import EgressInventory
from x.agentplane.app.identity import CallerIdentity, CallerKind, TokenReviewer, require_caller
from x.agentplane.app.inventory import SandboxInventory
from x.agentplane.app.live import LiveIndex
from x.agentplane.app.trajectory import TrajectoryStore
from x.agentplane.runner import protocol_pb2 as pb

MODELS = {Provider.CLAUDE: ["test-claude-model"], Provider.CODEX: ["test-codex-model"]}


async def test_action_api_enforces_caller_and_operator_scope(
    inventory: SandboxInventory,
    bridge: RunnerBridge,
    store: TrajectoryStore,
    egress: EgressInventory,
    decisions: DecisionsClient,
    live_index: LiveIndex,
    reviewer: TokenReviewer,
) -> None:
    hub = ActionHub(store.engine, EchoExecutor())
    await hub.ensure_schema()
    thread_id = await store.thread(
        "sandbox", "session", pb.SessionSpec(provider=pb.PROVIDER_CLAUDE, cwd="/work", model="test")
    )
    app = create_app(inventory, bridge, store, MODELS, egress, decisions, live_index, reviewer=reviewer, action_hub=hub)

    async def test_identity(request: Request) -> CallerIdentity:
        principal = request.headers.get("x-test-principal", "caller-a")
        kind = CallerKind.OPERATOR if principal == "operator" else CallerKind.TOKEN
        return CallerIdentity(kind, principal)

    app.dependency_overrides[require_caller] = test_identity
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as caller_a:
            created = await caller_a.post(
                "/actions",
                json={
                    "capability": EchoExecutor.CAPABILITY,
                    "arguments": {"message": "hello", "authorization": "do-not-return"},
                    "origin_thread_id": str(thread_id),
                },
            )
            assert created.status_code == 201, created.text
            receipt = created.json()
            assert receipt["state"] == "decision_pending"
            assert receipt["arguments"]["authorization"] == "[redacted]"
            request_id = receipt["id"]
            assert [row["id"] for row in (await caller_a.get("/actions")).json()] == [request_id]

        async with httpx.AsyncClient(
            transport=transport, base_url="http://test", headers={"x-test-principal": "caller-b"}
        ) as caller_b:
            assert (await caller_b.get("/actions")).json() == []
            assert (await caller_b.get(f"/actions/{request_id}")).status_code == 404
            assert (
                await caller_b.post(
                    f"/actions/{request_id}/decision",
                    json={"verdict": "allow", "expected_version": 1, "idempotency_key": "not-an-operator"},
                )
            ).status_code == 404

        async with httpx.AsyncClient(
            transport=transport, base_url="http://test", headers={"x-test-principal": "operator"}
        ) as operator:
            pending = await operator.get("/actions", params={"state": "decision_pending"})
            assert [row["id"] for row in pending.json()] == [request_id]
            allowed = await operator.post(
                f"/actions/{request_id}/decision",
                json={"verdict": "allow", "expected_version": 1, "idempotency_key": "operator-allow-1"},
            )
            assert allowed.status_code == 200, allowed.text
            decision = allowed.json()["decision"]
            assert decision["verdict"] == "allow"
            assert decision["provider"] == "human_operator"
            assert decision["issuer"] == "operator"
            assert decision["idempotency_key"] == "operator-allow-1"
            repeated = await operator.post(
                f"/actions/{request_id}/decision",
                json={"verdict": "allow", "expected_version": 1, "idempotency_key": "operator-allow-1"},
            )
            assert repeated.status_code == 200

            final = allowed.json()
            for _ in range(100):
                final = (await operator.get(f"/actions/{request_id}")).json()
                if final["state"] == "succeeded":
                    break
                await asyncio.sleep(0.01)
            assert final["state"] == "succeeded"
            assert final["execution"]["result"] == {"echo": {"message": "hello", "authorization": "[redacted]"}}
    finally:
        await hub.close()


async def test_action_submission_rejects_unknown_thread_and_capability(
    inventory: SandboxInventory,
    bridge: RunnerBridge,
    store: TrajectoryStore,
    egress: EgressInventory,
    decisions: DecisionsClient,
    live_index: LiveIndex,
    reviewer: TokenReviewer,
) -> None:
    hub = ActionHub(store.engine, EchoExecutor())
    await hub.ensure_schema()
    app = create_app(inventory, bridge, store, MODELS, egress, decisions, live_index, reviewer=reviewer, action_hub=hub)

    async def caller(_request: Request) -> CallerIdentity:
        return CallerIdentity(CallerKind.TOKEN, "caller")

    app.dependency_overrides[require_caller] = caller
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as http:
        missing = await http.post(
            "/actions",
            json={
                "capability": EchoExecutor.CAPABILITY,
                "arguments": {},
                "origin_thread_id": "00000000-0000-0000-0000-000000000000",
            },
        )
        unsupported = await http.post(
            "/actions",
            json={
                "capability": "mcp:invented.tool",
                "arguments": {},
                "origin_thread_id": "00000000-0000-0000-0000-000000000000",
            },
        )

    assert (missing.status_code, unsupported.status_code) == (422, 422)


if __name__ == "__main__":
    pytest_bazel.main()
