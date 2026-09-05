"""The HTTP contract over the inventory: status codes, bodies, and the schema's own validation."""

from __future__ import annotations

import socket
from collections.abc import Iterator

import httpx
import pytest
import pytest_bazel
from fastapi.testclient import TestClient

from x.agentplane.app.api import Provider, create_app
from x.agentplane.app.bridge import RunnerBridge
from x.agentplane.app.conftest import AGENT_AUTH
from x.agentplane.app.decisions import DecisionsClient
from x.agentplane.app.egress import EgressInventory
from x.agentplane.app.identity import TokenReviewer
from x.agentplane.app.inventory import ARCHIVED_LABEL, SandboxInventory
from x.agentplane.app.live import LiveIndex
from x.agentplane.app.presets import PresetCatalog, SandboxPreset, ThreadPreset
from x.agentplane.app.testing.egress_proxy import FakeEgressAdmin, decision
from x.agentplane.app.testing.kubernetes import (
    FakeCoreV1Api,
    FakeCustomObjectsApi,
    egress_binding,
    egress_policy,
    pod,
    sandbox,
)
from x.agentplane.app.trajectory import TrajectoryStore
from x.agentplane.runner import protocol_pb2 as pb

# TestClient drives the app over httpx, imported inside starlette; gazelle cannot see it.
# gazelle:include_dep @pypi//httpx
# The generated protocol stubs' own stub chain, which the mypy aspect resolves for direct deps only.
# gazelle:include_dep @pypi//protobuf


TEST_MODELS = {Provider.CLAUDE: ["test-claude-model"], Provider.CODEX: ["test-codex-model"]}
TEST_PRESETS = PresetCatalog(
    sandboxes={
        "public-coder": SandboxPreset(
            title="Public coder",
            template="agentplane-test-runner",
            policies=["github"],
            thread_preset="public-coder-codex",
            bootstrap="mkdir -p /state/workspaces",
        )
    },
    threads={
        "public-coder-codex": ThreadPreset(
            title="Public coder / Codex",
            provider=Provider.CODEX,
            model="test-codex-model",
            reasoning_effort="medium",
            instructions="preset instructions",
        )
    },
)


@pytest.fixture
def client(
    inventory: SandboxInventory,
    bridge: RunnerBridge,
    store: TrajectoryStore,
    egress: EgressInventory,
    decisions: DecisionsClient,
    live_index: LiveIndex,
    custom_objects: FakeCustomObjectsApi,
    core_v1: FakeCoreV1Api,
    reviewer: TokenReviewer,
) -> Iterator[TestClient]:
    custom_objects.objects[("sandboxes", "live")] = sandbox("live")
    core_v1.pods["live"] = pod("live", phase="Running", ready=True, ip="10.0.0.7")
    custom_objects.objects[("sandboxes", "fresh")] = sandbox("fresh")
    custom_objects.objects[("sandboxes", "shelved")] = sandbox(
        "shelved", labels={ARCHIVED_LABEL: "true"}, operating_mode="Suspended"
    )
    custom_objects.objects[("egresspolicies", "github")] = egress_policy(
        "github", [{"hosts": ["api.github.com"], "methods": ["GET"]}]
    )
    custom_objects.objects[("egresspolicies", "pypi")] = egress_policy("pypi", [{"hosts": ["pypi.org"]}])
    custom_objects.objects[("egressbindings", "live-seeded")] = egress_binding(
        "live-seeded", subjects=[{"sandbox": {"name": "live"}}], policies=["github"], active=("True", "Resolved", "")
    )
    custom_objects.objects[("egressbindings", "live-granted")] = egress_binding(
        "live-granted", subjects=[{"sandbox": {"name": "live"}}], policies=["pypi"], from_git=False
    )
    app = create_app(
        inventory, bridge, store, TEST_MODELS, egress, decisions, live_index, reviewer=reviewer, presets=TEST_PRESETS
    )
    with TestClient(app, headers=AGENT_AUTH) as test_client:
        yield test_client


def test_list_reports_state_and_hides_archived_by_default(client: TestClient) -> None:
    response = client.get("/sandboxes")

    assert response.status_code == 200
    assert {row["name"]: row["state"] for row in response.json()} == {"live": "running", "fresh": "waiting_for_pod"}
    assert {row["name"] for row in client.get("/sandboxes", params={"include_archived": "true"}).json()} == {
        "live",
        "fresh",
        "shelved",
    }


def test_get_returns_the_row_or_404(client: TestClient) -> None:
    row = client.get("/sandboxes/live").json()

    assert (row["state"], row["pod"]["ip"]) == ("running", "10.0.0.7")
    assert row["pod"]["containers"][0]["state"] == "running"
    assert client.get("/sandboxes/nope").status_code == 404


def test_create_returns_the_new_row(client: TestClient, custom_objects: FakeCustomObjectsApi) -> None:
    response = client.post("/sandboxes", json={"slug": "demo"})

    assert response.status_code == 201
    row = response.json()
    assert row["name"].startswith("demo-")
    assert row["state"] == "waiting_for_pod"
    assert ("sandboxes", row["name"]) in custom_objects.objects
    # Nothing was picked, so nothing may leave it.
    assert client.get(f"/sandboxes/{row['name']}/egress").json() == []


def test_create_with_preset_records_the_live_binding_and_allows_explicit_edits(
    client: TestClient, custom_objects: FakeCustomObjectsApi
) -> None:
    response = client.post(
        "/sandboxes",
        json={
            "slug": "coder",
            "preset": "public-coder",
            "thread_defaults": {"model": "edited-model", "instructions": "extra instructions"},
        },
    )

    assert response.status_code == 201, response.text
    row = response.json()
    assert row["preset_binding"] == {
        "sandbox_preset": "public-coder",
        "thread_preset": None,
        "thread_overrides": {
            "provider": None,
            "model": "edited-model",
            "cwd": None,
            "reasoning_effort": None,
            "instructions": "extra instructions",
        },
    }
    (binding,) = client.get(f"/sandboxes/{row['name']}/egress").json()
    assert [policy["name"] for policy in binding["policies"]] == ["github"]
    annotation = custom_objects.objects[("sandboxes", row["name"])]["metadata"]["annotations"]
    assert "agentplane.allegedly.works/launch-preset" in annotation


def test_explicit_sandbox_fields_replace_preset_defaults(client: TestClient) -> None:
    row = client.post("/sandboxes", json={"slug": "coder", "preset": "public-coder", "policies": ["pypi"]}).json()

    (binding,) = client.get(f"/sandboxes/{row['name']}/egress").json()
    assert [policy["name"] for policy in binding["policies"]] == ["pypi"]


def test_create_with_picked_policies_grants_one_binding_the_sandbox_owns(
    client: TestClient, custom_objects: FakeCustomObjectsApi
) -> None:
    response = client.post("/sandboxes", json={"slug": "demo", "policies": ["pypi"]})

    assert response.status_code == 201, response.text
    row = response.json()
    # The new sandbox's egress is its own pick and nothing else: no binding names it in advance.
    (binding,) = client.get(f"/sandboxes/{row['name']}/egress").json()
    assert [policy["name"] for policy in binding["policies"]] == ["pypi"]
    picked = custom_objects.objects[("egressbindings", binding["name"])]
    assert (
        picked["metadata"]["ownerReferences"][0]["uid"]
        == custom_objects.objects[("sandboxes", row["name"])]["metadata"]["uid"]
    )


@pytest.mark.parametrize("default_policies", [["github"]])
def test_a_default_policy_is_granted_whether_or_not_the_caller_picks_it(client: TestClient) -> None:
    """The model endpoint is what this is for in the deployment: without it a sandbox has no agent,
    so it is not the caller's to leave out — nor, having picked it, to be granted twice. The
    parameter overrides the `default_policies` fixture the client is built from."""
    unpicked = client.post("/sandboxes", json={"slug": "plain"}).json()
    (binding,) = client.get(f"/sandboxes/{unpicked['name']}/egress").json()
    assert [policy["name"] for policy in binding["policies"]] == ["github"]

    picked = client.post("/sandboxes", json={"slug": "asked", "policies": ["github", "pypi"]}).json()
    (binding,) = client.get(f"/sandboxes/{picked['name']}/egress").json()
    assert [policy["name"] for policy in binding["policies"]] == ["github", "pypi"]


@pytest.mark.parametrize(
    "body",
    [
        {"slug": "Demo"},
        {"slug": "-demo"},
        {"slug": "a" * 58},
        {"slug": "demo", "provider": "claude"},
        {"slug": "demo", "model": "cheap"},
    ],
    ids=[
        "uppercase-slug",
        "leading-dash-slug",
        "slug-too-long-for-a-dns-label",
        "provider-on-sandbox",
        "model-on-sandbox",
    ],
)
def test_create_rejects_invalid_requests(client: TestClient, custom_objects: FakeCustomObjectsApi, body: dict) -> None:
    response = client.post("/sandboxes", json=body)

    assert response.status_code == 422
    assert all(kind != "sandboxes" or name in {"live", "fresh", "shelved"} for kind, name in custom_objects.objects)


def test_suspend_resume_archive_unarchive_apply_in_order(
    client: TestClient, custom_objects: FakeCustomObjectsApi
) -> None:
    assert client.post("/sandboxes/live/suspend").status_code == 204
    assert client.get("/sandboxes/live").json()["state"] == "suspended"
    assert client.post("/sandboxes/live/resume").status_code == 204
    assert client.get("/sandboxes/live").json()["state"] == "running"
    assert client.post("/sandboxes/live/archive").status_code == 204
    assert client.get("/sandboxes/live").json()["state"] == "archived"
    assert client.post("/sandboxes/live/unarchive").status_code == 204
    assert client.get("/sandboxes/live").json()["state"] == "suspended"
    assert custom_objects.objects[("sandboxes", "live")]["spec"]["operatingMode"] == "Suspended"
    assert client.post("/sandboxes/nope/suspend").status_code == 404


def test_delete_removes_the_sandbox_once_it_is_suspended(
    client: TestClient, custom_objects: FakeCustomObjectsApi
) -> None:
    refused = client.delete("/sandboxes/live")

    assert refused.status_code == 409
    assert "suspend it" in refused.json()["detail"]
    assert ("sandboxes", "live") in custom_objects.objects

    assert client.post("/sandboxes/live/suspend").status_code == 204
    assert client.delete("/sandboxes/live").status_code == 204
    assert ("sandboxes", "live") not in custom_objects.objects
    assert client.delete("/sandboxes/live").status_code == 404


def test_bound_thread_defaults_resolve_before_bootstrap_and_explicit_launch_fields_win(
    client: TestClient, bridge: RunnerBridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    created = client.post(
        "/sandboxes", json={"slug": "coder", "preset": "public-coder", "thread_defaults": {"model": "sandbox-model"}}
    ).json()
    calls: list[tuple[str, object]] = []

    async def initialize(name: str, initialization: str, script: str) -> pb.InitializeResult:
        calls.append(("initialize", (initialization, script)))
        return pb.InitializeResult(key="0" * 64, executed=True)

    async def open_session(name: str, session_id: str, spec: pb.SessionSpec) -> pb.Attached:
        calls.append(("open", spec))
        return pb.Attached(session_id=session_id, spec=spec)

    monkeypatch.setattr(bridge, "initialize", initialize)
    monkeypatch.setattr(bridge, "open_session", open_session)

    response = client.post(
        f"/sandboxes/{created['name']}/sessions",
        json={"session_id": "thread-1", "spec": {"model": "thread-model", "instructions": "thread instructions"}},
    )

    assert response.status_code == 201, response.text
    assert calls[0] == ("initialize", ("sandbox-preset:public-coder", "mkdir -p /state/workspaces"))
    assert calls[1][0] == "open"
    spec = calls[1][1]
    assert isinstance(spec, pb.SessionSpec)
    assert (spec.provider, spec.cwd, spec.model, spec.reasoning_effort, spec.instructions) == (
        pb.PROVIDER_CODEX,
        "/state/workspaces/thread-1",
        "thread-model",
        "medium",
        "thread instructions",
    )


def test_no_preset_session_api_is_unchanged(
    client: TestClient, bridge: RunnerBridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: list[pb.SessionSpec] = []

    async def open_session(name: str, session_id: str, spec: pb.SessionSpec) -> pb.Attached:
        captured.append(spec)
        return pb.Attached(session_id=session_id, spec=spec)

    monkeypatch.setattr(bridge, "open_session", open_session)
    response = client.post(
        "/sandboxes/live/sessions",
        json={"session_id": "plain-1", "spec": {"provider": "PROVIDER_CLAUDE", "cwd": "/w", "model": "plain-model"}},
    )

    assert response.status_code == 201, response.text
    assert captured == [pb.SessionSpec(provider=pb.PROVIDER_CLAUDE, cwd="/w", model="plain-model")]


def test_a_runner_that_does_not_answer_is_a_503(
    inventory: SandboxInventory,
    store: TrajectoryStore,
    egress: EgressInventory,
    decisions: DecisionsClient,
    live_index: LiveIndex,
    custom_objects: FakeCustomObjectsApi,
    core_v1: FakeCoreV1Api,
    reviewer: TokenReviewer,
) -> None:
    """A Pod with an address but no runner listening yet, as right after a resume."""
    custom_objects.objects[("sandboxes", "live")] = sandbox("live")
    core_v1.pods["live"] = pod("live", phase="Running", ready=True, ip="10.0.0.7")

    # A bound but never listening port refuses every connection for as long as the socket is open.
    with socket.socket() as closed_port:
        closed_port.bind(("127.0.0.1", 0))
        address = f"127.0.0.1:{closed_port.getsockname()[1]}"

        async def nobody_listens(name: str) -> str:
            return address

        app = create_app(
            inventory,
            RunnerBridge(address_of=nobody_listens, store=store),
            store,
            TEST_MODELS,
            egress,
            decisions,
            live_index,
            reviewer=reviewer,
        )
        with TestClient(app, headers=AGENT_AUTH) as client:
            response = client.get("/sandboxes/live/sessions")
    assert response.status_code == 503
    assert "not answering" in response.json()["detail"]


def test_egress_lists_the_bindings_naming_the_sandbox(client: TestClient) -> None:
    response = client.get("/sandboxes/live/egress")

    assert response.status_code == 200, response.text
    body = response.json()
    assert [(b["name"], b["from_git"], b["active"]) for b in body] == [
        ("live-granted", False, None),
        ("live-seeded", True, True),
    ]
    assert body[1]["policies"][0]["rules"][0]["hosts"] == ["api.github.com"]
    # Nothing names `fresh`, so nothing may leave it.
    assert client.get("/sandboxes/fresh/egress").json() == []
    assert client.get("/sandboxes/nope/egress").status_code == 404


def test_granting_a_running_sandbox_adds_a_binding_and_leaves_the_others(
    client: TestClient, custom_objects: FakeCustomObjectsApi
) -> None:
    """A sandbox's egress is not frozen at creation: the grant is one more binding naming it, and
    revoking that one leaves the rest of its egress standing."""
    granted = client.post("/sandboxes/live/egress", json={"policies": ["pypi", "github"]})

    assert granted.status_code == 201, granted.text
    binding = granted.json()
    assert (binding["subjects"], binding["from_git"]) == (["live"], False)
    assert [policy["name"] for policy in binding["policies"]] == ["pypi", "github"]
    # Owned by the Sandbox, so deleting the sandbox takes the grant with it.
    (owner,) = custom_objects.objects[("egressbindings", binding["name"])]["metadata"]["ownerReferences"]
    assert (owner["kind"], owner["name"], owner["uid"]) == (
        "Sandbox",
        "live",
        custom_objects.objects[("sandboxes", "live")]["metadata"]["uid"],
    )
    assert sorted(b["name"] for b in client.get("/sandboxes/live/egress").json()) == sorted(
        ["live-granted", "live-seeded", binding["name"]]
    )

    assert client.delete(f"/egress/bindings/{binding['name']}").status_code == 204
    assert [b["name"] for b in client.get("/sandboxes/live/egress").json()] == ["live-granted", "live-seeded"]


def test_a_grant_naming_a_policy_that_does_not_exist_is_refused(
    client: TestClient, custom_objects: FakeCustomObjectsApi
) -> None:
    """422 and not 404: the sandbox in the path is there, and 404 on this route already says it is
    not. The CRD would admit the dangling name and the proxy would report `MissingPolicy`, so what
    the operator would otherwise get is a grant that grants nothing."""
    refused = client.post("/sandboxes/live/egress", json={"policies": ["github", "vanished"]})

    assert refused.status_code == 422
    assert "vanished" in refused.json()["detail"]
    assert [b["name"] for b in client.get("/sandboxes/live/egress").json()] == ["live-granted", "live-seeded"]
    assert client.post("/sandboxes/nope/egress", json={"policies": ["github"]}).status_code == 404
    # The CRD's policies are minItems: 1, so an empty grant is refused here rather than at admission.
    assert client.post("/sandboxes/live/egress", json={"policies": []}).status_code == 422
    # And at launch the names resolve before the Sandbox exists, so a typo leaves none behind.
    assert client.post("/sandboxes", json={"slug": "demo", "policies": ["vanished"]}).status_code == 422
    assert all(kind != "sandboxes" or name in {"live", "fresh", "shelved"} for kind, name in custom_objects.objects)


def test_egress_decisions_come_from_the_proxy(client: TestClient, egress_admin: FakeEgressAdmin) -> None:
    egress_admin.decisions["live"] = [
        decision("2026-09-02T10:00:00Z", "CONNECT", "api.github.com", None, "allow"),
        decision("2026-09-02T10:00:01Z", "GET", "api.github.com", "/repos/x/y", "allow", address="140.82.116.5"),
        decision("2026-09-02T10:00:02Z", "POST", "pypi.org", "/simple/", "deny", reason="no-rule"),
    ]

    response = client.get("/sandboxes/live/egress/decisions")

    assert response.status_code == 200, response.text
    assert [(d["method"], d["host"], d["path"], d["outcome"], d["reason"]) for d in response.json()] == [
        ("CONNECT", "api.github.com", None, "allow", None),
        ("GET", "api.github.com", "/repos/x/y", "allow", None),
        ("POST", "pypi.org", "/simple/", "deny", "no-rule"),
    ]
    # The proxy resolves and pins the host itself; the address it dialled reaches the page.
    assert [d["address"] for d in response.json()] == [None, "140.82.116.5", None]
    assert egress_admin.queries == ["live"]
    assert client.get("/sandboxes/nope/egress/decisions").status_code == 404
    assert egress_admin.queries == ["live"]


def test_egress_decisions_are_502_when_the_proxy_is_unreachable(
    client: TestClient, egress_admin: FakeEgressAdmin
) -> None:
    egress_admin.reachable = False

    response = client.get("/sandboxes/live/egress/decisions")

    assert response.status_code == 502
    assert "did not answer" in response.json()["detail"]
    assert [b["name"] for b in client.get("/sandboxes/live/egress").json()] == ["live-granted", "live-seeded"]


def test_every_route_needs_one_of_the_two_credentials(client: TestClient) -> None:
    """Guarded from the app, not from a proxy in front of it: no header a caller sets is trusted."""
    assert client.get("/sandboxes", headers={"Authorization": ""}).status_code == 401
    assert client.get("/sandboxes", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.post("/sandboxes", json={"slug": "x"}, headers={"Authorization": ""}).status_code == 401
    # The forgeable header the API server's service proxy used to forward buys nothing now.
    assert client.get("/sandboxes", headers={"Authorization": "", "x-authentik-username": "root"}).status_code == 401
    assert client.get("/healthz", headers={"Authorization": ""}).status_code == 204


def test_binding_revocation(client: TestClient) -> None:
    """A runtime binding is revoked by deleting it; one the manifest declares would be re-applied."""
    refused = client.delete("/egress/bindings/live-seeded")
    assert refused.status_code == 409
    assert "git" in refused.json()["detail"]
    assert client.delete("/egress/bindings/live-granted").status_code == 204
    assert client.delete("/egress/bindings/live-granted").status_code == 404
    assert [b["name"] for b in client.get("/sandboxes/live/egress").json()] == ["live-seeded"]


def test_policies_lists_the_namespace_for_the_create_form(client: TestClient) -> None:
    assert {policy["name"] for policy in client.get("/egress/policies").json()} == {"github", "pypi"}


def test_presets_publish_editable_sandbox_and_thread_defaults(client: TestClient) -> None:
    assert client.get("/presets").json() == [
        {
            "name": "public-coder",
            "title": "Public coder",
            "template": "agentplane-test-runner",
            "policies": ["github"],
            "thread_preset": "public-coder-codex",
            "thread_defaults": {
                "provider": "codex",
                "model": "test-codex-model",
                "cwd": "/state/workspaces/{session_id}",
                "reasoning_effort": "medium",
                "instructions": "preset instructions",
            },
        }
    ]


def test_models_lists_what_each_harness_may_run(client: TestClient) -> None:
    """The catalog the session form offers; a thread carries its model, a sandbox does not."""
    assert client.get("/models").json() == {"claude": ["test-claude-model"], "codex": ["test-codex-model"]}


async def test_a_thread_is_found_by_its_session_and_renamed_in_place(
    inventory: SandboxInventory,
    bridge: RunnerBridge,
    store: TrajectoryStore,
    egress: EgressInventory,
    decisions: DecisionsClient,
    live_index: LiveIndex,
    reviewer: TokenReviewer,
) -> None:
    """Over ASGI on this loop, not TestClient's thread: the store's pooled asyncpg connections
    belong to the loop that opened them."""
    spec = pb.SessionSpec(provider=pb.PROVIDER_CLAUDE, cwd="/w", model="test-model")
    thread_id = str(await store.thread("live", "s-1", spec))
    await store.thread("live", "s-2", spec)
    app = create_app(inventory, bridge, store, TEST_MODELS, egress, decisions, live_index, reviewer=reviewer)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test", headers=AGENT_AUTH
    ) as http:
        (found,) = (await http.get("/threads", params={"sandbox": "live", "session_id": "s-1"})).json()
        assert (found["id"], found["name"]) == (thread_id, None)
        assert (await http.get("/threads", params={"sandbox": "other"})).json() == []

        renamed = await http.patch(f"/threads/{thread_id}", json={"name": "  list the files  "})
        assert renamed.status_code == 200, renamed.text
        assert renamed.json()["name"] == "list the files"
        assert (await http.get(f"/threads/{thread_id}")).json()["name"] == "list the files"
        assert (await http.patch(f"/threads/{thread_id}", json={"name": "   "})).json()["name"] is None
        assert (await http.patch(f"/threads/{thread_id}", json={"name": None})).json()["name"] is None
        assert (await http.patch(f"/threads/{thread_id}", json={"name": "x" * 201})).status_code == 422
        assert (await http.patch(f"/threads/{thread_id}", json={})).status_code == 422
        missing = await http.patch("/threads/00000000-0000-0000-0000-000000000000", json={"name": "nobody"})
        assert missing.status_code == 404


def test_healthz_answers_outside_the_schema(client: TestClient) -> None:
    assert client.get("/healthz").status_code == 204
    assert "/healthz" not in client.get("/openapi.json").json()["paths"]


def test_openapi_schema_names_every_operation(client: TestClient) -> None:
    paths = client.get("/openapi.json").json()["paths"]

    assert set(paths) == {
        "/models",
        "/presets",
        "/sandboxes",
        "/sandboxes/{name}",
        "/sandboxes/{name}/suspend",
        "/sandboxes/{name}/resume",
        "/sandboxes/{name}/archive",
        "/sandboxes/{name}/unarchive",
        "/sandboxes/{name}/egress",
        "/sandboxes/{name}/egress/decisions",
        "/egress/policies",
        "/egress/bindings/{name}",
        "/sandboxes/{name}/sessions",
        "/sandboxes/{name}/sessions/{session_id}/events",
        "/sandboxes/{name}/sessions/{session_id}/inputs",
        "/sandboxes/{name}/sessions/{session_id}/interrupt",
        "/sandboxes/{name}/sessions/{session_id}/shutdown",
        "/threads",
        "/threads/{thread_id}",
        "/threads/{thread_id}/events",
        "/live/sandboxes",
        "/live/sandboxes/{name}",
    }
    assert set(paths["/sandboxes"]) == {"get", "post"}
    assert set(paths["/sandboxes/{name}"]) == {"get", "delete"}
    assert set(paths["/sandboxes/{name}/egress"]) == {"get", "post"}
    assert set(paths["/threads/{thread_id}"]) == {"get", "patch"}
    assert set(paths["/egress/bindings/{name}"]) == {"delete"}


if __name__ == "__main__":
    pytest_bazel.main()
