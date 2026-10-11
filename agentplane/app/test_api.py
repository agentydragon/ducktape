"""The HTTP contract over the inventory: status codes, bodies, and the schema's own validation."""

from __future__ import annotations

import asyncio
import socket
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast, override
from uuid import UUID, uuid4

import grpc
import httpx
import pytest
import pytest_bazel
from fastapi.testclient import TestClient

from agentplane.app.action_policy import ActionPolicyInventory
from agentplane.app.api import create_app, upstream_http_error
from agentplane.app.conftest import AGENT_AUTH
from agentplane.app.database_updates import DatabaseUpdates
from agentplane.app.decisions import DecisionsClient
from agentplane.app.egress_access import EgressAccess
from agentplane.app.electric import ElectricProxy
from agentplane.app.identity import CallerIdentity, CallerKind, TokenReviewer, require_caller
from agentplane.app.live import LiveIndex
from agentplane.app.model_catalog import ModelCatalog, ModelOption
from agentplane.app.operator_sessions import OperatorSessionStore
from agentplane.app.presets import PresetCatalog, SandboxPreset, ThreadPreset
from agentplane.app.testing.egress_proxy import FakeEgressAdmin, decision
from agentplane.app.testing.history import (
    ProjectedHistory as Ingestion,
    ProjectedIngester as Ingester,
    SeededEventLogStore as EventLogStore,
)
from agentplane.app.testing.model_test_data import TEST_REASONING_EFFORTS
from agentplane.app.threads.bridge import RunnerBridge
from agentplane.app.threads.sessions import SandboxSessions
from agentplane.app.threads.store import ThreadStore
from agentplane.app.threads.view.content import ContentStore
from agentplane.grpc_serving import serving
from agentplane.proto import event_log_pb2, runner_gateway_pb2_grpc, runner_pb2, sandbox_service_pb2
from agentplane.runner.harness import Harness
from agentplane.sandbox_service.client import SandboxServiceClient
from agentplane.sandbox_service.kubernetes_grants import KubernetesGrant, RoleBindingGrant, RoleRef
from agentplane.sandbox_service.testing.backend import Endpoint, backend, seed_runner
from agentplane.sandbox_service.testing.fake_inventory import (
    NAMESPACE,
    RUNNER_ENDPOINTS,
    TEMPLATE,
    FakeCoreV1Api,
    FakeCustomObjectsApi,
    action_policy_set,
    egress_binding,
    egress_policy,
    pod,
    sandbox,
)
from agentplane.sandbox_service.testing.fake_rbac import FakeRbac

# TestClient drives the app over httpx, imported inside starlette; gazelle cannot see it.
# gazelle:include_dep @pypi//httpx
# The generated protocol stubs' own stub chain, which the mypy aspect resolves for direct deps only.
# gazelle:include_dep @pypi//protobuf


TEST_MODELS = ModelCatalog(
    models=[
        ModelOption(
            model="test-claude-model", display_name="Test Claude Model", reasoning_efforts=list(TEST_REASONING_EFFORTS)
        ),
        ModelOption(
            model="test-codex-model", display_name="Test Codex Model", reasoning_efforts=list(TEST_REASONING_EFFORTS)
        ),
    ],
    harnesses={Harness.CLAUDE: ["test-claude-model"], Harness.CODEX: ["test-codex-model"]},
)


CODEX_ONLY = ModelCatalog(
    models=[option for option in TEST_MODELS.models if option.model == "test-codex-model"],
    harnesses={Harness.CLAUDE: [], Harness.CODEX: ["test-codex-model"]},
)


@pytest.fixture(params=[TEST_MODELS])
def model_catalog(request: pytest.FixtureRequest) -> ModelCatalog:
    return cast(ModelCatalog, request.param)


@pytest.mark.parametrize("upstream_status", [None, 403])
def test_upstream_http_error_preserves_request_without_secrets(upstream_status: int | None) -> None:
    request = httpx.Request(
        "POST",
        "https://user:private-password@identity-provider.invalid/token?code=private-code#private-fragment",
        headers={"Authorization": "Bearer private-token"},
    )
    error: httpx.HTTPStatusError | httpx.RequestError
    if upstream_status is None:
        error = httpx.ConnectError("private transport details", request=request)
    else:
        response = httpx.Response(upstream_status, request=request, text="private response body")
        error = httpx.HTTPStatusError("private status details", request=request, response=response)
    result = upstream_http_error(error)
    assert result.status_code == (upstream_status if upstream_status is not None else 503)
    detail: object = result.detail
    assert detail == {
        "method": "POST",
        "url": "https://identity-provider.invalid/token",
        "upstream_status": upstream_status,
        "error_type": "ConnectError" if upstream_status is None else "HTTPStatusError",
    }


TEST_PRESETS = PresetCatalog(
    sandboxes={
        "public-coder": SandboxPreset(
            title="Public coder",
            template="agentplane-test-runner",
            egress_policies=["github"],
            action_policy_sets=["github-reads"],
            thread_preset="public-coder-codex",
            bootstrap="mkdir -p /state/workspaces",
        )
    },
    threads={
        "public-coder-codex": ThreadPreset(
            title="Public coder / Codex",
            harness=Harness.CODEX,
            model="test-codex-model",
            reasoning_effort="medium",
            instructions="preset instructions",
        )
    },
)


@pytest.fixture
def client(
    model_catalog: ModelCatalog,
    inventory: SandboxServiceClient,
    bridge: RunnerBridge,
    store: ThreadStore,
    database_updates: DatabaseUpdates,
    operator_sessions: OperatorSessionStore,
    egress: EgressAccess,
    decisions: DecisionsClient,
    live_index: LiveIndex,
    action_policy: ActionPolicyInventory,
    custom_objects: FakeCustomObjectsApi,
    core_v1: FakeCoreV1Api,
    reviewer: TokenReviewer,
    electric_proxy: ElectricProxy,
    notifications_http: httpx.AsyncClient,
    notifications_token_file: Path,
    event_logs: EventLogStore,
    content: ContentStore,
) -> Iterator[TestClient]:
    custom_objects.objects[("sandboxes", "live")] = sandbox("live")
    core_v1.pods["live"] = pod("live", phase="Running", ready=True, ip="10.0.0.7")
    custom_objects.objects[("sandboxes", "fresh")] = sandbox("fresh")
    custom_objects.objects[("egresspolicies", "github")] = egress_policy(
        "github", [{"hosts": ["api.github.com"], "methods": ["GET"]}]
    )
    custom_objects.objects[("egresspolicies", "pypi")] = egress_policy("pypi", [{"hosts": ["pypi.org"]}])
    custom_objects.objects[("egressbindings", "live-seeded")] = egress_binding(
        "live-seeded",
        subjects=[{"namespace": NAMESPACE, "name": "live"}],
        policies=["github"],
        active=("True", "Resolved", ""),
    )
    custom_objects.objects[("egressbindings", "live-granted")] = egress_binding(
        "live-granted", subjects=[{"namespace": NAMESPACE, "name": "live"}], policies=["pypi"], from_git=False
    )
    custom_objects.objects[("actionpolicysets", "github-reads")] = action_policy_set(
        "github-reads",
        auto_approve_if=[{"type": "exact_actions", "actions": {"github": ["search_code"]}}],
        ready=("True", "Valid", "spec accepted"),
    )
    custom_objects.objects[("actionpolicysets", "github-writes")] = action_policy_set(
        "github-writes", auto_approve_if=[{"type": "exact_actions", "actions": {"github": ["push_files"]}}]
    )
    app = create_app(
        inventory,
        bridge,
        store,
        model_catalog,
        egress,
        decisions,
        live_index,
        action_policy,
        reviewer=reviewer,
        presets=TEST_PRESETS,
        electric=electric_proxy,
        event_logs=event_logs,
        content=content,
        database_updates=database_updates,
        operator_sessions=operator_sessions,
        notifications_http=notifications_http,
        notifications_token_file=notifications_token_file,
    )
    with TestClient(app, headers=AGENT_AUTH) as test_client:
        yield test_client


def test_list_reports_separate_resource_statuses(client: TestClient) -> None:
    response = client.get("/sandboxes")

    assert response.status_code == 200
    rows = {row["name"]: row for row in response.json()}
    assert rows["live"]["operating_mode"] == "Running"
    assert rows["live"]["pod"]["status"]["phase"] == "Running"
    assert rows["fresh"]["operating_mode"] == "Running"
    assert rows["fresh"]["pod"] is None


def test_get_returns_the_row_or_404(client: TestClient) -> None:
    row = client.get("/sandboxes/live").json()

    assert row["operating_mode"] == "Running"
    assert row["pod"]["name"] == "live"
    assert row["pod"]["status"]["podIP"] == "10.0.0.7"
    assert row["pod"]["status"]["containerStatuses"][0]["state"] == {"running": {}}
    assert client.get("/sandboxes/nope").status_code == 404


def test_create_returns_the_new_row(client: TestClient, custom_objects: FakeCustomObjectsApi) -> None:
    response = client.post("/sandboxes", json={"name": "demo", "template": TEMPLATE})

    assert response.status_code == 201
    row = response.json()
    assert row["name"] == "demo"
    assert row["operating_mode"] == "Suspended"
    assert row["initializing"] is True
    assert row["pod"] is None
    assert ("sandboxes", row["name"]) in custom_objects.objects
    # Nothing was picked, so nothing may leave it.
    assert client.get(f"/sandboxes/{row['name']}/egress").json() == []


def test_templates_list_the_concrete_choices_for_the_create_form(client: TestClient) -> None:
    assert client.get("/sandboxes/templates").json() == [TEMPLATE]


def test_create_records_the_concrete_session_defaults_and_bootstrap(
    client: TestClient, sandbox_endpoint: Endpoint, custom_objects: FakeCustomObjectsApi
) -> None:
    response = client.post(
        "/sandboxes",
        json={
            "name": "coder",
            "template": TEMPLATE,
            "egress_policies": ["github"],
            "action_policy_sets": ["github-reads"],
            "session_defaults": {
                "harness": "HARNESS_CODEX",
                "model": "edited-model",
                "cwd": "/state/workspaces/{session_id}",
                "reasoning_effort": "medium",
                "instructions": "extra instructions",
            },
            "bootstrap": "mkdir -p /state/workspaces",
        },
    )

    assert response.status_code == 201, response.text
    row = response.json()
    assert row["binding"] == {
        "session_defaults": {
            "harness": "HARNESS_CODEX",
            "model": "edited-model",
            "cwd": "/state/workspaces/{session_id}",
            "reasoning_effort": "medium",
            "instructions": "extra instructions",
            "setup_script": None,
        },
        "bootstrap": "mkdir -p /state/workspaces",
    }
    sandbox_endpoint.reconcile()
    (binding,) = client.get(f"/sandboxes/{row['name']}/egress").json()
    assert [policy["name"] for policy in binding["policies"]] == [*RUNNER_ENDPOINTS.egress_policies(), "github"]
    annotation = custom_objects.objects[("sandboxes", row["name"])]["metadata"]["annotations"]
    assert "agentplane.allegedly.works/sandbox-binding" in annotation


def test_create_binds_the_sandbox_to_its_action_policy_sets(
    client: TestClient, sandbox_endpoint: Endpoint, custom_objects: FakeCustomObjectsApi
) -> None:
    """One ActionPolicyBinding per launched Sandbox, owned by it and naming the ServiceAccount it
    runs as: what its harness may do without the operator, as the Action Service reads it. Reading the policy back is the Action Service's answer through the operator
    federation (`test_action_api.py`), so a token caller is refused it."""
    row = client.post(
        "/sandboxes", json={"name": "coder", "template": TEMPLATE, "action_policy_sets": ["github-reads"]}
    ).json()

    sandbox_endpoint.reconcile()
    sandbox_uid = custom_objects.objects[("sandboxes", row["name"])]["metadata"]["uid"]
    (written,) = [obj for (kind, _), obj in custom_objects.objects.items() if kind == "actionpolicybindings"]
    assert written["metadata"]["name"] == f"ap-init-{UUID(sandbox_uid).hex}"
    assert written["metadata"]["labels"] == {"app.agentplane.allegedly.works/managed-by": "integration-app"}
    assert written["metadata"]["ownerReferences"][0]["uid"] == sandbox_uid
    assert written["spec"] == {"subject": {"namespace": NAMESPACE, "name": row["name"]}, "policySets": ["github-reads"]}

    client.post("/sandboxes", json={"name": "plain", "template": TEMPLATE})
    sandbox_endpoint.reconcile()
    assert len([kind for kind, _ in custom_objects.objects if kind == "actionpolicybindings"]) == 1


def test_kubernetes_grant_picker_requires_an_approved_name_and_operator(
    client: TestClient, custom_objects: FakeCustomObjectsApi, core_v1: FakeCoreV1Api
) -> None:
    app = cast(Any, client.app)
    app.state.kubernetes_grants = {
        "config": RoleBindingGrant(
            kind="RoleBinding", namespace=NAMESPACE, role_ref=RoleRef(kind="Role", name="config-reader")
        )
    }
    choices = client.get("/kubernetes-grants")
    assert choices.status_code == 200
    assert [(row["name"], row["namespace"], row["role_ref"]["name"]) for row in choices.json()] == [
        ("config", NAMESPACE, "config-reader")
    ]
    before = set(core_v1.service_accounts)
    for body, status_code in (
        ({"kubernetes_grants": ["missing"]}, 422),
        ({"kubernetes_grants": ["config"]}, 403),
        ({"kubernetes_grants": ["config"], "role_ref": {"kind": "Role", "name": "admin"}}, 422),
    ):
        response = client.post("/sandboxes", json={"name": "coder", "template": TEMPLATE, **body})
        assert response.status_code == status_code, response.text
    assert set(core_v1.service_accounts) == before
    assert not any(kind == "rolebindings" for kind, _ in custom_objects.objects)


def test_operator_launch_provisions_the_selected_role_for_its_actual_sandbox_account(
    client: TestClient,
    sandbox_endpoint: Endpoint,
    sandbox_grants: dict[str, KubernetesGrant],
    sandbox_rbac: FakeRbac,
    custom_objects: FakeCustomObjectsApi,
) -> None:
    app = cast(Any, client.app)
    app.state.kubernetes_grants = {
        "config": RoleBindingGrant(
            kind="RoleBinding", namespace=NAMESPACE, role_ref=RoleRef(kind="Role", name="config-reader")
        )
    }
    sandbox_grants.update(app.state.kubernetes_grants)
    app.dependency_overrides[require_caller] = lambda: CallerIdentity(CallerKind.OPERATOR, "test-operator")
    try:
        response = client.post(
            "/sandboxes", json={"name": "coder", "template": TEMPLATE, "kubernetes_grants": ["config"]}
        )
    finally:
        app.dependency_overrides.pop(require_caller)
    assert response.status_code == 201, response.text
    row = response.json()
    assert row["initializing"] is True
    sandbox_endpoint.reconcile()
    row = client.get(f"/sandboxes/{row['name']}").json()
    assert row["kubernetes_grants_ready"] is True
    assert row["kubernetes_grants"][0]["grant"]["role_ref"] == {"kind": "Role", "name": "config-reader"}
    (bound,) = sandbox_rbac.bindings.values()
    assert bound.role_ref.name == "config-reader"
    assert [(subject.namespace, subject.name) for subject in bound.subjects] == [(NAMESPACE, row["name"])]
    stored = custom_objects.objects[("sandboxes", row["name"])]
    assert stored["metadata"]["annotations"]["agentplane.allegedly.works/kubernetes-grants"]


def _written_bindings(custom_objects: FakeCustomObjectsApi) -> list[tuple[str, list[str]]]:
    """Each ActionPolicyBinding the app wrote, as (bound account, its sets): the launch's whole effect,
    read where it landed, since the policy route answers only an operator session."""
    return [
        (obj["spec"]["subject"]["name"], obj["spec"]["policySets"])
        for (kind, _), obj in custom_objects.objects.items()
        if kind == "actionpolicybindings"
    ]


def test_the_launch_pick_of_action_policy_sets_is_the_operators(
    client: TestClient, sandbox_endpoint: Endpoint, custom_objects: FakeCustomObjectsApi
) -> None:
    """An explicit empty list binds nothing, while a concrete choice binds exactly that list."""
    unbound = client.post("/sandboxes", json={"name": "coder", "template": TEMPLATE, "action_policy_sets": []})
    assert unbound.status_code == 201, unbound.text
    sandbox_endpoint.reconcile()
    assert _written_bindings(custom_objects) == []

    picked = client.post(
        "/sandboxes",
        json={"name": "plain", "template": TEMPLATE, "action_policy_sets": ["github-reads", "github-writes"]},
    )
    sandbox_endpoint.reconcile()
    assert picked.status_code == 201, picked.text
    assert _written_bindings(custom_objects) == [(picked.json()["name"], ["github-reads", "github-writes"])]

    sandboxes_before = [name for kind, name in custom_objects.objects if kind == "sandboxes"]
    refused = client.post(
        "/sandboxes", json={"name": "vanished", "template": TEMPLATE, "action_policy_sets": ["vanished"]}
    )
    assert refused.status_code == 422, refused.text
    assert [name for kind, name in custom_objects.objects if kind == "sandboxes"] == sandboxes_before


def test_policy_sets_list_the_namespace_for_the_create_form(client: TestClient) -> None:
    sets = {policy_set["name"]: policy_set for policy_set in client.get("/action-policy/sets").json()}
    assert set(sets) == {"github-reads", "github-writes"}
    assert sets["github-reads"]["ready"]["status"] == "True"
    assert (sets["github-writes"]["ready"], sets["github-writes"]["refused"]) == (None, None)


def test_create_with_picked_policies_grants_one_binding_the_sandbox_owns(
    client: TestClient, sandbox_endpoint: Endpoint, custom_objects: FakeCustomObjectsApi
) -> None:
    response = client.post("/sandboxes", json={"name": "demo", "template": TEMPLATE, "egress_policies": ["pypi"]})

    assert response.status_code == 201, response.text
    row = response.json()
    sandbox_endpoint.reconcile()
    # The new sandbox's egress is its runner's endpoints and its own pick: no binding names it in advance.
    (binding,) = client.get(f"/sandboxes/{row['name']}/egress").json()
    assert [policy["name"] for policy in binding["policies"]] == [*RUNNER_ENDPOINTS.egress_policies(), "pypi"]
    picked = custom_objects.objects[("egressbindings", binding["name"])]
    assert (
        picked["metadata"]["ownerReferences"][0]["uid"]
        == custom_objects.objects[("sandboxes", row["name"])]["metadata"]["uid"]
    )


@pytest.mark.parametrize("default_egress_policies", [["github"]])
def test_a_default_policy_is_granted_whether_or_not_the_caller_picks_it(
    client: TestClient, sandbox_endpoint: Endpoint
) -> None:
    """The model endpoint is what this is for in the deployment: without it a sandbox has no agent,
    so it is not the caller's to leave out — nor, having picked it, to be granted twice. The
    parameter overrides the `default_egress_policies` fixture the client is built from."""
    unpicked = client.post("/sandboxes", json={"name": "plain", "template": TEMPLATE}).json()
    sandbox_endpoint.reconcile()
    (binding,) = client.get(f"/sandboxes/{unpicked['name']}/egress").json()
    assert [policy["name"] for policy in binding["policies"]] == [*RUNNER_ENDPOINTS.egress_policies(), "github"]

    picked = client.post(
        "/sandboxes", json={"name": "asked", "template": TEMPLATE, "egress_policies": ["github", "pypi"]}
    ).json()
    sandbox_endpoint.reconcile()
    (binding,) = client.get(f"/sandboxes/{picked['name']}/egress").json()
    assert [policy["name"] for policy in binding["policies"]] == [*RUNNER_ENDPOINTS.egress_policies(), "github", "pypi"]


@pytest.mark.parametrize(
    "body",
    [
        {"name": "demo"},
        {"name": "Demo", "template": TEMPLATE},
        {"name": "-demo", "template": TEMPLATE},
        {"name": "a" * 58, "template": TEMPLATE},
        {"name": "demo", "template": TEMPLATE, "harness": "claude"},
    ],
    ids=["no-template", "uppercase-name", "leading-dash-name", "name-too-long-for-a-dns-label", "harness-on-sandbox"],
)
def test_create_rejects_invalid_requests(client: TestClient, custom_objects: FakeCustomObjectsApi, body: dict) -> None:
    response = client.post("/sandboxes", json=body)

    assert response.status_code == 422
    assert all(kind != "sandboxes" or name in {"live", "fresh"} for kind, name in custom_objects.objects)


def test_suspend_resume_apply_in_order(client: TestClient, custom_objects: FakeCustomObjectsApi) -> None:
    assert client.post("/sandboxes/live/suspend").status_code == 204
    assert client.get("/sandboxes/live").json()["operating_mode"] == "Suspended"
    assert client.post("/sandboxes/live/resume").status_code == 204
    assert client.get("/sandboxes/live").json()["operating_mode"] == "Running"
    assert custom_objects.objects[("sandboxes", "live")]["spec"]["operatingMode"] == "Running"
    assert client.post("/sandboxes/nope/suspend").status_code == 404


def test_delete_starts_teardown_once_the_sandbox_is_suspended(
    client: TestClient, custom_objects: FakeCustomObjectsApi
) -> None:
    assert client.delete("/sandboxes/live").status_code == 409
    assert not client.get("/sandboxes/live").json()["deleting"]

    assert client.post("/sandboxes/live/suspend").status_code == 204
    assert client.delete("/sandboxes/live").status_code == 204
    # The Sandbox Service deletes it once its runner has sealed every Session.
    assert client.get("/sandboxes/live").json()["deleting"]
    assert client.delete("/sandboxes/live").status_code == 204
    assert client.delete("/sandboxes/nope").status_code == 404


def test_bound_thread_forwards_overrides_without_app_bootstrap_or_default_assembly(
    client: TestClient, bridge: RunnerBridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    created = client.post(
        "/sandboxes",
        json={
            "name": "coder",
            "template": TEMPLATE,
            "session_defaults": {
                "harness": "HARNESS_CODEX",
                "model": "sandbox-model",
                "cwd": "/state/workspaces/{session_id}",
                "reasoning_effort": "medium",
                "instructions": "preset instructions",
                "setup_script": "printf 'preset setup\\n'",
            },
            "bootstrap": "mkdir -p /state/workspaces",
        },
    ).json()
    calls: list[tuple[str, object]] = []
    setup_scripts: list[str | None] = []

    async def create_session(
        name: str, session_id: UUID, spec: dict[str, object], setup_script: str | None = None
    ) -> runner_pb2.Attached:
        calls.append(("create", spec))
        setup_scripts.append(setup_script)
        return runner_pb2.Attached(session_id=str(uuid4()))

    monkeypatch.setattr(bridge, "create_session", create_session)

    response = client.post(
        f"/sandboxes/{created['name']}/sessions",
        json={"session_id": str(uuid4()), "spec": {"model": "thread-model", "instructions": "thread instructions"}},
    )

    assert response.status_code == 201, response.text
    assert calls == [("create", {"model": "thread-model", "instructions": "thread instructions"})]
    assert setup_scripts == [None]
    cleared = client.post(
        f"/sandboxes/{created['name']}/sessions", json={"session_id": str(uuid4()), "spec": {}, "setup_script": ""}
    )
    assert cleared.status_code == 201, cleared.text
    assert setup_scripts == [None, ""]


@pytest.mark.parametrize("model_catalog", [CODEX_ONLY], indirect=True)
def test_thread_resume_for_regular_sandbox_reaches_the_bridge(
    client: TestClient, bridge: RunnerBridge, store: ThreadStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert client.get("/models").json()["harnesses"]["HARNESS_CLAUDE"] == []
    thread_id = UUID("be4017d0-8994-4a36-b58c-5e6e264d0119")

    async def get_thread(_thread_id: UUID) -> Any:
        return SimpleNamespace(sandbox="live", archived=False, harness=Harness.CLAUDE, cwd="/w")

    calls: list[tuple[UUID, Harness | None, str | None]] = []

    async def resume_thread(
        thread: UUID, *, expected_harness: Harness | None, expected_cwd: str | None
    ) -> runner_pb2.Attached:
        calls.append((thread, expected_harness, expected_cwd))
        return runner_pb2.Attached(session_id="regular-session")

    monkeypatch.setattr(store, "get_thread", get_thread)
    monkeypatch.setattr(bridge, "resume_thread", resume_thread)
    response = client.post(f"/threads/{thread_id}/resume")

    assert response.status_code == 200, response.text
    assert calls == [(thread_id, Harness.CLAUDE, "/w")]


@pytest.mark.parametrize("model_catalog", [CODEX_ONLY], indirect=True)
def test_direct_session_launch_leaves_platform_instructions_to_service(
    client: TestClient, bridge: RunnerBridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: list[dict[str, object]] = []

    async def create_session(
        name: str, session_id: UUID, spec: dict[str, object], setup_script: str | None = None
    ) -> runner_pb2.Attached:
        captured.append(spec)
        return runner_pb2.Attached(session_id=str(uuid4()))

    monkeypatch.setattr(bridge, "create_session", create_session)
    response = client.post(
        "/sandboxes/live/sessions",
        json={"session_id": str(uuid4()), "spec": {"harness": "HARNESS_CLAUDE", "cwd": "/w", "model": "plain-model"}},
    )

    assert response.status_code == 201, response.text
    assert captured == [{"harness": "HARNESS_CLAUDE", "cwd": "/w", "model": "plain-model"}]


@pytest.mark.parametrize(("error_type", "status_code"), [(TimeoutError, 504), (ConnectionError, 503)])
def test_service_launch_failure_preserves_uncertainty(
    client: TestClient,
    bridge: RunnerBridge,
    monkeypatch: pytest.MonkeyPatch,
    error_type: type[Exception],
    status_code: int,
) -> None:
    async def failed_create(
        name: str, session_id: UUID, spec: dict[str, object], setup_script: str | None = None
    ) -> runner_pb2.Attached:
        raise error_type("test upstream failure")

    monkeypatch.setattr(bridge, "create_session", failed_create)
    response = client.post(
        "/sandboxes/live/sessions",
        json={"session_id": str(uuid4()), "spec": {"harness": "HARNESS_CLAUDE", "cwd": "/w", "model": "m"}},
    )
    assert response.status_code == status_code


def test_create_session_answers_the_service_id(
    client: TestClient, bridge: RunnerBridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The client's Session ID reaches the bridge; the answer carries the ID the service confirmed."""
    requested = uuid4()
    public_id = str(uuid4())
    received: list[tuple[str, UUID, dict[str, object], str | None]] = []

    async def create_session(
        sandbox: str, session_id: UUID, spec: dict[str, object], setup_script: str | None = None
    ) -> runner_pb2.Attached:
        received.append((sandbox, session_id, spec, setup_script))
        return runner_pb2.Attached(session_id=public_id)

    monkeypatch.setattr(bridge, "create_session", create_session)
    path = "/sandboxes/live/sessions"
    created = client.post(
        path, json={"session_id": str(requested), "spec": {"cwd": "/w/{session_id}"}, "setup_script": "bootstrap"}
    )
    assert created.status_code == 201, created.text
    assert created.json()["sessionId"] == public_id
    assert received == [("live", requested, {"cwd": "/w/{session_id}"}, "bootstrap")]
    assert client.post(path, json={"session_id": "not-a-uuid", "spec": {}}).status_code == 422


async def test_open_lookup_reconciles_only_runner_confirmed_sessions(
    client: TestClient, bridge: RunnerBridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No reservation/runner confirmation means no Thread; a confirmed one repairs lost app writes."""
    # Keep DB work on the async fixture's loop, not TestClient's portal thread.
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=client.app), base_url="http://testserver", headers=AGENT_AUTH
    ) as http:
        public_id = str(uuid4())
        summary = runner_pb2.SessionSummary(
            session_id=public_id,
            spec=runner_pb2.SessionSpec(harness=runner_pb2.HARNESS_CLAUDE, cwd="/w", model="m", instructions="secret"),
        )
        result = sandbox_service_pb2.LookupSessionResponse()
        calls: list[str] = []

        async def lookup(*, idempotency_key: str) -> sandbox_service_pb2.LookupSessionResponse:
            calls.append(idempotency_key)
            return result

        monkeypatch.setattr(bridge._runners, "client", lambda name: SimpleNamespace(lookup=lookup))
        path = "/sandboxes/live/sessions/open"
        requested = str(uuid4())
        absent = await http.get(path, params={"session_id": requested})
        assert absent.status_code == 200
        assert absent.json() == {"status": "absent", "session_id": None}
        assert (await http.get(f"/threads/{public_id}")).status_code == 404

        result.session_id = public_id
        unconfirmed = await http.get(path, params={"session_id": requested})
        assert unconfirmed.json() == {"status": "unconfirmed", "session_id": public_id}
        assert (await http.get(f"/threads/{public_id}")).status_code == 404

        result.failed = True
        failed = await http.get(path, params={"session_id": requested})
        assert failed.json() == {"status": "failed", "session_id": public_id}
        assert (await http.get(f"/threads/{public_id}")).status_code == 404

        result.failed = False
        result.summary.CopyFrom(summary)
        for _ in range(2):  # Reconcile after a lost app mapping, then repeat idempotently.
            ready = await http.get(path, params={"session_id": requested})
            assert ready.status_code == 200, ready.text
            assert ready.json() == {"status": "ready", "session_id": public_id}
            assert "secret" not in ready.text
        assert (await http.get(f"/threads/{public_id}")).json()["sandbox"] == "live"
        assert calls == [requested] * 5
        assert (await http.get(path, params={"session_id": "not-a-uuid"})).status_code == 422


def test_a_runner_that_does_not_answer_is_a_503(
    tmp_path: Path,
    custom_objects: FakeCustomObjectsApi,
    core_v1: FakeCoreV1Api,
    inventory: SandboxServiceClient,
    store: ThreadStore,
    database_updates: DatabaseUpdates,
    operator_sessions: OperatorSessionStore,
    egress: EgressAccess,
    decisions: DecisionsClient,
    live_index: LiveIndex,
    action_policy: ActionPolicyInventory,
    reviewer: TokenReviewer,
    event_logs: EventLogStore,
    content: ContentStore,
    ingestion: Ingestion,
    electric_proxy: ElectricProxy,
    notifications_http: httpx.AsyncClient,
    notifications_token_file: Path,
    service_history_db_url: str,
) -> None:
    """A Pod with an address but no runner listening yet, as right after a resume."""
    live_index.sandboxes["live"], live_index.pods["live"] = seed_runner(custom_objects, core_v1, "live")

    # A bound but never listening port refuses every connection for as long as the socket is open.
    with socket.socket() as closed_port:
        closed_port.bind(("127.0.0.1", 0))
        with backend(
            custom_objects,
            core_v1,
            tmp_path / "failure-token",
            database_url=service_history_db_url,
            runner_port=closed_port.getsockname()[1],
        ) as endpoint:
            runners = SandboxSessions(live_index, endpoint.client())
            app = create_app(
                inventory,
                RunnerBridge(
                    runners=runners,
                    event_logs=event_logs,
                    content=content,
                    ingester=Ingester(runners=runners, event_logs=event_logs, ingestion=ingestion),
                ),
                store,
                TEST_MODELS,
                egress,
                decisions,
                live_index,
                action_policy,
                reviewer=reviewer,
                event_logs=event_logs,
                content=content,
                database_updates=database_updates,
                operator_sessions=operator_sessions,
                electric=electric_proxy,
                notifications_http=notifications_http,
                notifications_token_file=notifications_token_file,
            )
            with TestClient(app, headers=AGENT_AUTH) as client:
                response = client.get("/sandboxes/live/sessions")
    assert response.status_code == 503
    assert "UNAVAILABLE" in response.json()["detail"]


@asynccontextmanager
async def unanswered_command_service(
    token_file: Path,
) -> AsyncIterator[tuple[SandboxServiceClient, asyncio.Queue[str]]]:
    class UnansweredGateway(runner_gateway_pb2_grpc.RunnerGatewayServicer):
        @override
        async def SubmitCommand(
            self, request: sandbox_service_pb2.SubmitCommandRequest, context: grpc.aio.ServicerContext
        ) -> event_log_pb2.EventEntry:
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.put_nowait(request.destination.session_id)
            raise AssertionError("the command is never answered")

    cancelled: asyncio.Queue[str] = asyncio.Queue()
    server = grpc.aio.server()
    runner_gateway_pb2_grpc.add_RunnerGatewayServicer_to_server(UnansweredGateway(), server)
    await asyncio.to_thread(token_file.write_text, "test-app-command-peer")
    async with (
        serving(server, "127.0.0.1:0", grace_s=0) as port,
        SandboxServiceClient(
            f"127.0.0.1:{port}",
            gateway_target=f"127.0.0.1:{port}",
            namespace="test",
            token_file=token_file,
            gateway_token_file=token_file,
            command_admission_timeout_s=1,
            request_timeout_s=5,
            lifecycle_timeout_s=5,
            follow_timeout_s=5,
        ) as service,
    ):
        yield service, cancelled


async def test_unanswered_service_command_is_a_504_and_cancels_its_rpc(
    tmp_path: Path,
    custom_objects: FakeCustomObjectsApi,
    core_v1: FakeCoreV1Api,
    inventory: SandboxServiceClient,
    store: ThreadStore,
    database_updates: DatabaseUpdates,
    operator_sessions: OperatorSessionStore,
    egress: EgressAccess,
    decisions: DecisionsClient,
    live_index: LiveIndex,
    action_policy: ActionPolicyInventory,
    reviewer: TokenReviewer,
    event_logs: EventLogStore,
    content: ContentStore,
    ingestion: Ingestion,
    electric_proxy: ElectricProxy,
    notifications_http: httpx.AsyncClient,
    notifications_token_file: Path,
) -> None:
    """Bound command admission at the public service RPC, without runner internals."""
    live_index.sandboxes["live"], live_index.pods["live"] = seed_runner(custom_objects, core_v1, "live")
    session_id = str(uuid4())
    thread_id = await event_logs.open("live", session_id)
    async with unanswered_command_service(tmp_path / "wedged-token") as (service, cancelled):
        runners = SandboxSessions(live_index, service)
        ingester = Ingester(runners=runners, event_logs=event_logs, ingestion=ingestion)
        app = create_app(
            inventory,
            RunnerBridge(runners=runners, event_logs=event_logs, content=content, ingester=ingester),
            store,
            TEST_MODELS,
            egress,
            decisions,
            live_index,
            action_policy,
            reviewer=reviewer,
            event_logs=event_logs,
            content=content,
            database_updates=database_updates,
            operator_sessions=operator_sessions,
            electric=electric_proxy,
            notifications_http=notifications_http,
            notifications_token_file=notifications_token_file,
        )
        try:
            async with (
                httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://test", headers=AGENT_AUTH
                ) as http,
                asyncio.timeout(10),
            ):
                response = await http.post(
                    f"/threads/{thread_id}/commands",
                    json={"commandId": "test-unanswered-command", "submitInput": {"text": "never admitted"}},
                )
                assert response.status_code == 504, response.text
                assert await cancelled.get() == session_id
        finally:
            await ingester.close()


def test_egress_lists_the_bindings_naming_the_sandbox(client: TestClient) -> None:
    response = client.get("/sandboxes/live/egress")

    assert response.status_code == 200, response.text
    body = response.json()
    assert [(b["name"], b["from_git"]) for b in body] == [("live-granted", False), ("live-seeded", True)]
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
    assert (binding["subjects"], binding["from_git"]) == ([{"namespace": NAMESPACE, "name": "live"}], False)
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
    assert client.post("/sandboxes/live/egress", json={"policies": ["github", "vanished"]}).status_code == 422
    assert [b["name"] for b in client.get("/sandboxes/live/egress").json()] == ["live-granted", "live-seeded"]
    assert client.post("/sandboxes/nope/egress", json={"policies": ["github"]}).status_code == 404
    # The CRD's policies are minItems: 1, so an empty grant is refused here rather than at admission.
    assert client.post("/sandboxes/live/egress", json={"policies": []}).status_code == 422
    # And at launch the names resolve before the Sandbox exists, so a typo leaves none behind.
    assert (
        client.post(
            "/sandboxes", json={"name": "demo", "template": TEMPLATE, "egress_policies": ["vanished"]}
        ).status_code
        == 422
    )
    assert all(kind != "sandboxes" or name in {"live", "fresh"} for kind, name in custom_objects.objects)


def test_egress_decisions_come_from_the_proxy(client: TestClient, egress_admin: FakeEgressAdmin) -> None:
    egress_admin.decisions[(NAMESPACE, "live")] = [
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
    assert egress_admin.queries == [(NAMESPACE, "live")], (
        "the route asks for the account the sandbox runs as, namespace included"
    )
    assert client.get("/sandboxes/nope/egress/decisions").status_code == 404
    assert egress_admin.queries == [(NAMESPACE, "live")]


def test_egress_decisions_are_502_when_the_proxy_is_unreachable(
    client: TestClient, egress_admin: FakeEgressAdmin
) -> None:
    egress_admin.reachable = False

    assert client.get("/sandboxes/live/egress/decisions").status_code == 502
    assert [b["name"] for b in client.get("/sandboxes/live/egress").json()] == ["live-granted", "live-seeded"]


def test_every_route_needs_one_of_the_two_credentials(client: TestClient) -> None:
    """Guarded from the app, not from a proxy in front of it: no header a caller sets is trusted."""
    assert client.get("/sandboxes", headers={"Authorization": ""}).status_code == 401
    assert client.get("/sandboxes", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.post("/sandboxes", json={"name": "x"}, headers={"Authorization": ""}).status_code == 401
    # The forgeable header the API server's service proxy used to forward buys nothing now.
    assert client.get("/sandboxes", headers={"Authorization": "", "x-authentik-username": "root"}).status_code == 401
    assert client.get("/healthz", headers={"Authorization": ""}).status_code == 204


def test_thread_sync_routes_authenticate_before_dispatch(client: TestClient) -> None:
    path = "/threads/00000000-0000-0000-0000-000000000000/sync/scope"
    assert client.get(path, headers={"Authorization": ""}).status_code == 401


def test_binding_revocation(client: TestClient) -> None:
    """A runtime binding is revoked by deleting it; one the manifest declares would be re-applied."""
    assert client.delete("/egress/bindings/live-seeded").status_code == 409
    assert client.delete("/egress/bindings/live-granted").status_code == 204
    assert client.delete("/egress/bindings/live-granted").status_code == 404
    assert [b["name"] for b in client.get("/sandboxes/live/egress").json()] == ["live-seeded"]


def test_policies_lists_the_namespace_for_the_create_form(client: TestClient) -> None:
    assert {policy["name"] for policy in client.get("/egress/policies").json()} == {
        "github",
        "pypi",
        *RUNNER_ENDPOINTS.egress_policies(),
    }


def test_presets_publish_editable_sandbox_and_session_defaults(client: TestClient) -> None:
    assert client.get("/presets").json() == [
        {
            "name": "public-coder",
            "title": "Public coder",
            "template": "agentplane-test-runner",
            "egress_policies": ["github"],
            "action_policy_sets": ["github-reads"],
            "kubernetes_grants": [],
            "session_defaults": {
                "harness": "HARNESS_CODEX",
                "model": "test-codex-model",
                "cwd": "/state/workspaces/{session_id}",
                "reasoning_effort": "medium",
                "instructions": "preset instructions",
                "setup_script": "",
            },
            "bootstrap": "mkdir -p /state/workspaces",
        }
    ]


def test_models_lists_what_each_harness_may_run(client: TestClient) -> None:
    """The catalog the session form offers; a thread carries its model, a sandbox does not."""
    assert client.get("/models").json() == {
        "models": [
            {
                "model": "test-claude-model",
                "display_name": "Test Claude Model",
                "reasoning_efforts": list(TEST_REASONING_EFFORTS),
            },
            {
                "model": "test-codex-model",
                "display_name": "Test Codex Model",
                "reasoning_efforts": list(TEST_REASONING_EFFORTS),
            },
        ],
        "harnesses": {"HARNESS_CLAUDE": ["test-claude-model"], "HARNESS_CODEX": ["test-codex-model"]},
    }


async def test_a_thread_is_found_by_its_session_and_renamed_in_place(
    inventory: SandboxServiceClient,
    bridge: RunnerBridge,
    store: ThreadStore,
    event_logs: EventLogStore,
    database_updates: DatabaseUpdates,
    operator_sessions: OperatorSessionStore,
    egress: EgressAccess,
    decisions: DecisionsClient,
    live_index: LiveIndex,
    action_policy: ActionPolicyInventory,
    reviewer: TokenReviewer,
    content: ContentStore,
    electric_proxy: ElectricProxy,
    notifications_http: httpx.AsyncClient,
    notifications_token_file: Path,
) -> None:
    """Over ASGI on this loop, not TestClient's thread: the store's pooled asyncpg connections
    belong to the loop that opened them."""
    thread_id = str(await event_logs.open("live", "s-1"))
    await event_logs.open("live", "s-2")
    app = create_app(
        inventory,
        bridge,
        store,
        TEST_MODELS,
        egress,
        decisions,
        live_index,
        action_policy,
        reviewer=reviewer,
        event_logs=event_logs,
        content=content,
        database_updates=database_updates,
        operator_sessions=operator_sessions,
        electric=electric_proxy,
        notifications_http=notifications_http,
        notifications_token_file=notifications_token_file,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test", headers=AGENT_AUTH
    ) as http:
        found = (await http.get(f"/threads/{thread_id}")).json()
        assert (found["id"], found["name"]) == (thread_id, None)
        assert (await http.get("/threads", params={"sandbox": "other"})).json()["threads"] == []

        renamed = await http.patch(f"/threads/{thread_id}", json={"name": "  list the files  "})
        assert renamed.status_code == 200, renamed.text
        assert renamed.json()["name"] == "list the files"
        assert (await http.get(f"/threads/{thread_id}")).json()["name"] == "list the files"
        assert (await http.patch(f"/threads/{thread_id}", json={"name": "   "})).json()["name"] is None
        assert (await http.patch(f"/threads/{thread_id}", json={"name": None})).json()["name"] is None
        missing = await http.patch("/threads/00000000-0000-0000-0000-000000000000", json={"name": "nobody"})
        assert missing.status_code == 404


async def test_a_thread_archives_and_unarchives_and_hides_from_the_default_listing(
    inventory: SandboxServiceClient,
    bridge: RunnerBridge,
    store: ThreadStore,
    event_logs: EventLogStore,
    database_updates: DatabaseUpdates,
    operator_sessions: OperatorSessionStore,
    egress: EgressAccess,
    decisions: DecisionsClient,
    live_index: LiveIndex,
    action_policy: ActionPolicyInventory,
    reviewer: TokenReviewer,
    content: ContentStore,
    electric_proxy: ElectricProxy,
    notifications_http: httpx.AsyncClient,
    notifications_token_file: Path,
) -> None:
    thread_id = str(await event_logs.open("live", "s-1"))
    app = create_app(
        inventory,
        bridge,
        store,
        TEST_MODELS,
        egress,
        decisions,
        live_index,
        action_policy,
        reviewer=reviewer,
        event_logs=event_logs,
        content=content,
        database_updates=database_updates,
        operator_sessions=operator_sessions,
        electric=electric_proxy,
        notifications_http=notifications_http,
        notifications_token_file=notifications_token_file,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test", headers=AGENT_AUTH
    ) as http:
        assert (await http.post(f"/threads/{thread_id}/archive")).status_code == 204
        assert (await http.get("/threads", params={"sandbox": "live"})).json()["threads"] == []
        [archived] = (await http.get("/threads", params={"sandbox": "live", "include_archived": "true"})).json()[
            "threads"
        ]
        assert archived["archived"] is True
        assert (await http.get(f"/threads/{thread_id}")).json()["archived"] is True

        assert (await http.post(f"/threads/{thread_id}/unarchive")).status_code == 204
        [unarchived] = (await http.get("/threads", params={"sandbox": "live"})).json()["threads"]
        assert unarchived["archived"] is False

        assert (await http.post("/threads/00000000-0000-0000-0000-000000000000/archive")).status_code == 404
        assert (await http.post("/threads/00000000-0000-0000-0000-000000000000/unarchive")).status_code == 404


async def test_a_running_thread_cannot_be_archived(
    inventory: SandboxServiceClient,
    bridge: RunnerBridge,
    store: ThreadStore,
    event_logs: EventLogStore,
    database_updates: DatabaseUpdates,
    operator_sessions: OperatorSessionStore,
    egress: EgressAccess,
    decisions: DecisionsClient,
    live_index: LiveIndex,
    action_policy: ActionPolicyInventory,
    reviewer: TokenReviewer,
    content: ContentStore,
    custom_objects: FakeCustomObjectsApi,
    core_v1: FakeCoreV1Api,
    monkeypatch: pytest.MonkeyPatch,
    electric_proxy: ElectricProxy,
    notifications_http: httpx.AsyncClient,
    notifications_token_file: Path,
) -> None:
    custom_objects.objects[("sandboxes", "live")], core_v1.pods["live"] = seed_runner(custom_objects, core_v1, "live")
    spec = runner_pb2.SessionSpec(harness=runner_pb2.HARNESS_CLAUDE, cwd="/w", model="test-model")
    thread_id = await event_logs.open("live", "s-1")

    consulted: list[str] = []

    async def running_sessions(sandbox: str) -> list[runner_pb2.SessionSummary]:
        consulted.append(sandbox)
        return [
            runner_pb2.SessionSummary(
                session_id=str(thread_id), spec=spec, harness_state=runner_pb2.HARNESS_STATE_RUNNING
            )
        ]

    monkeypatch.setattr(bridge, "list_sessions", running_sessions)
    app = create_app(
        inventory,
        bridge,
        store,
        TEST_MODELS,
        egress,
        decisions,
        live_index,
        action_policy,
        reviewer=reviewer,
        event_logs=event_logs,
        content=content,
        database_updates=database_updates,
        operator_sessions=operator_sessions,
        electric=electric_proxy,
        notifications_http=notifications_http,
        notifications_token_file=notifications_token_file,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test", headers=AGENT_AUTH
    ) as http:
        response = await http.post(f"/threads/{thread_id}/archive")

    assert response.status_code == 409
    # Only the running-harness refusal lists sessions; the unverifiable-Pod 409 comes before that.
    assert consulted == ["live"]
    thread = await store.get_thread(thread_id)
    assert thread is not None
    assert thread.archived is False


@pytest.mark.parametrize(
    "url",
    [
        "/sandboxes/ready-sandbox/notifications/stream",
        "/threads/notices/stream?thread_id=00000000-0000-4000-8000-000000000001",
    ],
)
def test_notification_diagnostics_require_operator_login(client: TestClient, url: str) -> None:
    # Workload authentication of the app is not operator authorization to inspect another inbox.
    response = client.get(url)
    assert response.status_code == 403
    assert response.json() == {"detail": "operator login required"}


if __name__ == "__main__":
    pytest_bazel.main()
