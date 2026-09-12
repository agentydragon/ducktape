"""The pushed views against the fetched ones, the staleness verdict, the frames a tab reads, and
what a sandbox frame says when the Action Service cannot be asked."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import httpx
import pytest
import pytest_bazel
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from util.net import pick_free_port
from x.agentplane.action_service.operator_oidc import OperatorOidcSettings
from x.agentplane.app.action_federation import DirectFederationSettings, FederatedOperatorActions
from x.agentplane.app.action_policy import ActionPolicyInventory, ActionPolicyUnavailable, ActionPolicyView
from x.agentplane.app.api import Provider, create_app
from x.agentplane.app.bridge import RunnerBridge, SandboxNotReachableError
from x.agentplane.app.decisions import DecisionsClient
from x.agentplane.app.egress import EgressInventory
from x.agentplane.app.identity import CallerIdentity, CallerKind, TokenReviewer
from x.agentplane.app.inventory import SANDBOXES_PLURAL, ProvisioningState, SandboxInventory
from x.agentplane.app.live import (
    PODS_PLURAL,
    ActionPolicyFrames,
    LiveIndex,
    SandboxesSnapshot,
    WatchHealth,
    action_policy_frame,
    frames,
)
from x.agentplane.app.oidc import OIDCSettings
from x.agentplane.app.testing.kubernetes import (
    FakeCoreV1Api,
    FakeCustomObjectsApi,
    action_policy_binding,
    action_policy_set,
    egress_binding,
    egress_policy,
    pod,
    sandbox,
)
from x.agentplane.app.trajectory import TrajectoryStore

# TestClient drives the app over httpx, imported inside starlette; gazelle cannot see it.
# gazelle:include_dep @pypi//httpx

NOW = datetime(2026, 9, 3, 12, 0, tzinfo=UTC)
MODELS = {Provider.CLAUDE: ["test-claude-model"], Provider.CODEX: ["test-codex-model"]}


@pytest.fixture
def seeded(custom_objects: FakeCustomObjectsApi, core_v1: FakeCoreV1Api, live_index: LiveIndex) -> LiveIndex:
    """The same objects in the fake API server and in the index, so the two paths can be compared."""
    custom_objects.objects[(SANDBOXES_PLURAL, "runner-1")] = sandbox("runner-1")
    custom_objects.objects[(SANDBOXES_PLURAL, "shelved")] = sandbox("shelved", operating_mode="Suspended")
    core_v1.pods["runner-1"] = pod("runner-1", phase="Running", ready=True, ip="10.0.0.7")
    custom_objects.objects[("egresspolicies", "github")] = egress_policy(
        "github", [{"hosts": ["api.github.com"], "methods": ["GET"]}]
    )
    custom_objects.objects[("egressbindings", "runner-1-picked")] = egress_binding(
        "runner-1-picked", subjects=[{"sandbox": {"name": "runner-1"}}], policies=["github"]
    )
    custom_objects.objects[("egressbindings", "elsewhere")] = egress_binding(
        "elsewhere", subjects=[{"sandbox": {"name": "shelved"}}], policies=["github"]
    )
    custom_objects.objects[("actionpolicysets", "reads")] = action_policy_set(
        "reads",
        auto_approve_if=[{"type": "exact_actions", "actions": {"github": ["search_code"]}}],
        ready=("True", "Valid", "spec accepted"),
    )
    runner_uid = custom_objects.objects[(SANDBOXES_PLURAL, "runner-1")]["metadata"]["uid"]
    custom_objects.objects[("actionpolicybindings", "runner-1-reads")] = action_policy_binding(
        "runner-1-reads", subject={"sandbox": {"name": "runner-1", "uid": runner_uid}}, policy_sets=["reads"]
    )
    for (kind, name), obj in custom_objects.objects.items():
        match kind:
            case "sandboxes":
                live_index.sandboxes[name] = obj
            case "egressbindings":
                live_index.bindings[name] = obj
            case "egresspolicies":
                live_index.policies[name] = obj
            case "actionpolicysets":
                live_index.action_policy_seen(live_index.action_policy_sets, name, obj)
            case "actionpolicybindings":
                live_index.action_policy_seen(live_index.action_policy_bindings, name, obj)
    live_index.pods.update(core_v1.pods)
    return live_index


async def test_the_index_projects_the_rows_a_listing_would_return(
    seeded: LiveIndex, inventory: SandboxInventory
) -> None:
    """The push and the fetch share their projection; this is what says they still do."""
    assert seeded.sandbox_views() == await inventory.list_sandboxes()
    assert seeded.sandbox_view("runner-1") == await inventory.get("runner-1")


async def test_the_index_selects_the_bindings_a_request_would(seeded: LiveIndex, egress: EgressInventory) -> None:
    assert seeded.bindings_for("runner-1") == await egress.bindings_for("runner-1")
    assert [binding.name for binding in seeded.bindings_for("runner-1")] == ["runner-1-picked"]


EMPTY_POLICY = ActionPolicyView(synced=True, bindings=[], auto_approve_if=[], auto_deny_if=[], auto_deny_unless=[])


async def test_the_sandbox_stream_asks_the_service_again_only_when_a_policy_object_changed(seeded: LiveIndex) -> None:
    """Every other change -- a Pod, a thread -- repeats the last answer; a failure is never repeated,
    and another incarnation of the sandbox (a new UID) is asked about afresh."""
    answers: list[ActionPolicyView | ActionPolicyUnavailable] = [
        ActionPolicyUnavailable(code="test-first-attempt-failed"),
        EMPTY_POLICY,
        EMPTY_POLICY.model_copy(update={"synced": False}),
        EMPTY_POLICY,
    ]
    asked: list[UUID] = []

    async def fetch(uid: UUID) -> ActionPolicyView | ActionPolicyUnavailable:
        asked.append(uid)
        return answers.pop(0)

    uid, reborn = uuid4(), uuid4()
    policy = ActionPolicyFrames(seeded, fetch)

    assert await policy.for_sandbox(uid) == ActionPolicyUnavailable(code="test-first-attempt-failed")
    assert await policy.for_sandbox(uid) == EMPTY_POLICY  # retried: the failure was not kept
    assert await policy.for_sandbox(uid) == EMPTY_POLICY  # nothing changed: repeated, not asked
    seeded.action_policy_seen(seeded.action_policy_bindings, "runner-1-reads", None)
    assert await policy.for_sandbox(uid) == EMPTY_POLICY.model_copy(update={"synced": False})
    assert await policy.for_sandbox(reborn) == EMPTY_POLICY
    assert asked == [uid, uid, uid, reborn]


def _request(app: FastAPI, session: dict[str, object]) -> Request:
    """A request as the session middleware hands it on: the login's own dict under `user`."""
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/live/sandboxes/runner-1",
            "headers": [],
            "app": app,
            "session": {"user": session},
        }
    )


@pytest.mark.parametrize(
    ("caller", "configured", "code"),
    [
        (CallerIdentity(CallerKind.TOKEN, "test-agent"), True, "operator_session_required"),
        (CallerIdentity(CallerKind.OPERATOR, "test-operator"), False, "operator_federation_not_configured"),
        (CallerIdentity(CallerKind.OPERATOR, "test-operator"), True, "operator_federation_token_invalid"),
    ],
)
async def test_a_policy_the_service_cannot_be_asked_for_is_said_so_in_the_frame(
    app: FastAPI, action_policy: ActionPolicyInventory, caller: CallerIdentity, configured: bool, code: str
) -> None:
    """The failure the route would answer with, as a frame variant a tab can show: a token caller
    is not an operator, an app without federation has nobody to ask as, and a session whose token
    the provider refuses is refused with that code and nothing of the provider's own text."""
    oidc = OIDCSettings(
        issuer="https://login.test.invalid/application/o/test-app/",
        client_id="test-app",
        client_secret="test-only-client-secret",
        session_secret="test-only-session-secret",
        public_base_url="http://test-app.invalid",
    )
    app.state.operator_actions = (
        FederatedOperatorActions(
            DirectFederationSettings(
                service_url="http://test-actions.invalid",
                login_jwks_uri=f"http://127.0.0.1:{pick_free_port()}/jwks",
                target=OperatorOidcSettings(
                    issuer="https://login.test.invalid/application/o/actions/",
                    audience="test-actions",
                    jwks_uri=f"http://127.0.0.1:{pick_free_port()}/jwks",
                ),
                scope="openid",
            ),
            oidc,
            httpx.AsyncClient(base_url="http://test-actions.invalid"),
        )
        if configured
        else None
    )
    session: dict[str, object] = {
        "issuer": oidc.issuer,
        "subject": "test-operator-subject",
        "username": "test-operator",
        "access_token": "test-not-a-jwt",
        "expires_at": time.time() + 600,
    }

    frame = await action_policy_frame(_request(app, session), caller, action_policy, uuid4())

    assert frame == ActionPolicyUnavailable(code=code)


def test_a_sandbox_the_watch_has_dropped_is_gone_rather_than_missing(seeded: LiveIndex) -> None:
    del seeded.sandboxes["runner-1"]

    assert seeded.sandbox_view("runner-1") is None


def test_nothing_watched_yet_reads_as_stale(live_index: LiveIndex) -> None:
    """A process whose first cycle never completed must not read as fresh for having no age."""
    assert live_index.health(NOW).fresh is False


def test_one_kind_that_stopped_cycling_makes_the_whole_frame_stale(live_index: LiveIndex) -> None:
    live_index.refreshed[SANDBOXES_PLURAL] = NOW - timedelta(seconds=5)
    live_index.refreshed[PODS_PLURAL] = NOW - timedelta(seconds=5)
    assert live_index.health(NOW).fresh is True

    live_index.refreshed[PODS_PLURAL] = NOW - timedelta(seconds=live_index.stale_after_seconds + 1)
    health = live_index.health(NOW)

    assert health.fresh is False
    assert health.refreshed_seconds_ago[PODS_PLURAL] > health.stale_after_seconds


async def test_a_frame_goes_out_per_change_with_health_through_the_quiet(seeded: LiveIndex) -> None:
    """What a tab reads: the state on connect, the state again when it changes, and in between the
    freshness that tells a quiet stream from a wedged one."""
    seeded.refreshed[SANDBOXES_PLURAL] = NOW
    stream = frames(lambda: _snapshot(seeded), lambda: seeded.health(NOW), seeded.changes, interval_s=0.01).__aiter__()

    assert _read(await anext(stream)) == ("snapshot", ["runner-1", "shelved"])
    assert _read(await anext(stream))[0] == "health"
    del seeded.sandboxes["runner-1"]
    seeded.changes.notify()

    assert await asyncio.wait_for(_next_snapshot(stream), timeout=5) == ["shelved"]


@pytest.fixture
def app(
    inventory: SandboxInventory,
    egress: EgressInventory,
    decisions: DecisionsClient,
    live_index: LiveIndex,
    action_policy: ActionPolicyInventory,
    reviewer: TokenReviewer,
) -> FastAPI:
    """Neither test below reaches a database or a runner -- the guard answers before a route body
    runs, and the document comes from the signatures -- so the engine here never connects."""

    async def unreachable(name: str) -> str:
        raise SandboxNotReachableError(name, ProvisioningState.WAITING_FOR_POD)

    store = TrajectoryStore.connect("postgresql+asyncpg://live-test@127.0.0.1:1/live-test")
    bridge = RunnerBridge(address_of=unreachable, store=store)
    return create_app(inventory, bridge, store, MODELS, egress, decisions, live_index, action_policy, reviewer=reviewer)


def test_the_streams_need_a_caller(app: FastAPI) -> None:
    with TestClient(app) as unauthenticated:
        assert unauthenticated.get("/live/sandboxes").status_code == 401
        assert unauthenticated.get("/live/sandboxes/runner-1").status_code == 401


def test_the_frame_models_are_published_in_the_document(app: FastAPI) -> None:
    """The SPA's types are generated from this document, so the frames it reads have to be in it."""
    with TestClient(app) as client:
        schemas = client.get("/openapi.json").json()["components"]["schemas"]

    assert {"SandboxesSnapshot", "SandboxSnapshot", "WatchHealth", "ActionPolicyUnavailable"} <= set(schemas)


async def _snapshot(index: LiveIndex) -> SandboxesSnapshot:
    return SandboxesSnapshot(sandboxes=index.sandbox_views(), watch=index.health(NOW))


def _read(frame: bytes) -> tuple[str, object]:
    """One SSE frame as (event, the sandbox names it carries or its health)."""
    event, data = (line.split(": ", 1)[1] for line in frame.decode().strip().splitlines())
    payload = json.loads(data)
    if event == "health":
        return event, WatchHealth.model_validate(payload)
    return event, [row["name"] for row in payload["sandboxes"]]


async def _next_snapshot(stream: AsyncIterator[bytes]) -> object:
    while True:
        event, payload = _read(await anext(stream))
        if event == "snapshot":
            return payload


if __name__ == "__main__":
    pytest_bazel.main()
