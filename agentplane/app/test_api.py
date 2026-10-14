"""The HTTP contract over the inventory: status codes, bodies, and the schema's own validation."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast, override
from uuid import UUID, uuid4

import grpc
import httpx
import pytest
import pytest_bazel
from fastapi import FastAPI

from agentplane.app.action_policy import ActionPolicyInventory
from agentplane.app.api import _require_operator_login, create_app, upstream_http_error
from agentplane.app.conftest import AGENT_AUTH
from agentplane.app.egress_access import EgressAccess
from agentplane.app.electric import ElectricProxy
from agentplane.app.identity import TokenReviewer
from agentplane.app.live import LiveIndex
from agentplane.app.model_catalog import ModelCatalog
from agentplane.app.operator_sessions import OperatorSessionStore
from agentplane.app.presets import PresetCatalog, SandboxPreset, ThreadPreset
from agentplane.app.sandbox_access import SandboxAccess
from agentplane.app.testing.history import SeededEventLogStore
from agentplane.app.testing.model_test_data import TEST_MODELS
from agentplane.app.testing.system_status import answering_system_status
from agentplane.app.threads.bridge import RunnerBridge
from agentplane.app.threads.runner_directory import RunnerDirectory
from agentplane.app.threads.store import ThreadStore
from agentplane.app.threads.view.content import ContentStore
from agentplane.egress.decisions_client import DecisionsClient
from agentplane.grpc_calls import dial
from agentplane.grpc_serving import serving
from agentplane.policy_sets import POLICY_BINDINGS_PLURAL, POLICY_SETS_PLURAL
from agentplane.postgres.wakeups import WakeupListener
from agentplane.proto import runner_gateway_pb2, runner_gateway_pb2_grpc, runner_pb2
from agentplane.runner.harness import Harness
from agentplane.runner_gateway.client import RunnerGatewayClient, SandboxControlClient
from agentplane.sandbox_resource import SANDBOXES_PLURAL
from agentplane.sandbox_service.client import SandboxServiceClient
from agentplane.sandbox_service.testing.backend import backend, seed_runner
from agentplane.sandboxes.inventory import SandboxInventory
from agentplane.sandboxes.testing.fake_inventory import (
    NAMESPACE,
    READY,
    TEMPLATE,
    FakeCoreV1Api,
    FakeCustomObjectsApi,
    action_policy_set,
    egress_policy_set,
    pod,
    policy_binding,
    sandbox,
)
from agentplane.subjects import ServiceAccountRef

# The generated protocol stubs' own stub chain, which the mypy aspect resolves for direct deps only.
# gazelle:include_dep @pypi//protobuf


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
            policy_sets=["basic"],
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
def presets() -> PresetCatalog:
    return TEST_PRESETS


@pytest.fixture
def bare_app(
    presets: PresetCatalog,
    model_catalog: ModelCatalog,
    inventory: SandboxServiceClient,
    bridge: RunnerBridge,
    store: ThreadStore,
    database_updates: WakeupListener,
    operator_sessions: OperatorSessionStore,
    egress: EgressAccess,
    decisions: DecisionsClient,
    live_index: LiveIndex,
    action_policy: ActionPolicyInventory,
    reviewer: TokenReviewer,
    electric_proxy: ElectricProxy,
    notifications_http: httpx.AsyncClient,
    notifications_token_file: Path,
    event_logs: SeededEventLogStore,
    content: ContentStore,
) -> FastAPI:
    """The app over the shared fixtures, with no Kubernetes object seeded."""
    return create_app(
        inventory,
        bridge,
        store,
        model_catalog,
        egress,
        decisions,
        live_index,
        action_policy,
        reviewer=reviewer,
        presets=presets,
        electric=electric_proxy,
        event_logs=event_logs,
        content=content,
        database_updates=database_updates,
        operator_sessions=operator_sessions,
        notifications_http=notifications_http,
        notifications_token_file=notifications_token_file,
        system_status=answering_system_status(),
    )


@pytest.fixture
def app(bare_app: FastAPI, custom_objects: FakeCustomObjectsApi, core_v1: FakeCoreV1Api) -> FastAPI:
    custom_objects.objects[(SANDBOXES_PLURAL, "live")] = sandbox("live", conditions=READY)
    core_v1.pods["live"] = pod("live", phase="Running", ready=True, ip="10.0.0.7")
    custom_objects.objects[(SANDBOXES_PLURAL, "fresh")] = sandbox("fresh")
    custom_objects.objects[(POLICY_SETS_PLURAL, "github")] = egress_policy_set(
        "github", [{"hosts": ["api.github.com"], "methods": ["GET"]}]
    )
    custom_objects.objects[(POLICY_SETS_PLURAL, "pypi")] = egress_policy_set("pypi", [{"hosts": ["pypi.org"]}])
    custom_objects.objects[(POLICY_BINDINGS_PLURAL, "live-seeded")] = policy_binding(
        "live-seeded", subject={"namespace": NAMESPACE, "name": "live"}, policy_sets=["github"]
    )
    custom_objects.objects[(POLICY_BINDINGS_PLURAL, "live-granted")] = policy_binding(
        "live-granted", subject={"namespace": NAMESPACE, "name": "live"}, policy_sets=["pypi"], from_git=False
    )
    for name in ("packages", "basic"):
        custom_objects.objects[(POLICY_SETS_PLURAL, name)] = egress_policy_set(name, [])
    custom_objects.objects[(POLICY_SETS_PLURAL, "github-reads")] = action_policy_set(
        "github-reads", [{"type": "exact_actions", "actions": {"github": ["search_code"]}}]
    )
    custom_objects.objects[(POLICY_SETS_PLURAL, "github-writes")] = action_policy_set(
        "github-writes", [{"type": "exact_actions", "actions": {"github": ["push_files"]}}]
    )
    custom_objects.objects[(POLICY_SETS_PLURAL, "broken-reads")] = action_policy_set(
        "broken-reads", [{"type": "no_such_policy_type"}]
    )
    return bare_app


def _agent_client(app: FastAPI) -> httpx.AsyncClient:
    """The app over ASGI on the test's own loop, where its gRPC channels and database pools were opened."""
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", headers=AGENT_AUTH)


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with _agent_client(app) as test_client:
        yield test_client


async def test_list_reports_separate_resource_statuses(client: httpx.AsyncClient) -> None:
    response = await client.get("/sandboxes")

    assert response.status_code == 200
    rows = {row["name"]: row for row in response.json()}
    assert rows["live"]["operating_mode"] == "Running"
    assert rows["live"]["pod"]["status"]["phase"] == "Running"
    assert rows["fresh"]["operating_mode"] == "Running"
    assert rows["fresh"]["pod"] is None


async def test_get_returns_the_row_or_404(client: httpx.AsyncClient) -> None:
    row = (await client.get("/sandboxes/live")).json()

    assert row["operating_mode"] == "Running"
    assert row["pod"]["name"] == "live"
    assert row["pod"]["status"]["podIP"] == "10.0.0.7"
    assert row["pod"]["status"]["containerStatuses"][0]["state"] == {"running": {}}
    assert (await client.get("/sandboxes/nope")).status_code == 404


async def test_create_returns_the_new_row(client: httpx.AsyncClient, custom_objects: FakeCustomObjectsApi) -> None:
    response = await client.post("/sandboxes", json={"name": "demo", "template": TEMPLATE})

    assert response.status_code == 201
    row = response.json()
    assert row["name"] == "demo"
    assert row["operating_mode"] == "Running"
    assert row["pod"] is None
    assert (SANDBOXES_PLURAL, row["name"]) in custom_objects.objects
    # Nothing was picked, so nothing may leave it.
    assert (await client.get(f"/sandboxes/{row['name']}/egress")).json() == []


async def test_templates_list_the_concrete_choices_for_the_create_form(client: httpx.AsyncClient) -> None:
    assert (await client.get("/sandboxes/templates")).json() == [TEMPLATE]


async def test_the_form_offers_the_namespace_policy_sets(client: httpx.AsyncClient) -> None:
    assert (await client.get("/sandboxes/form")).json() == {
        "policy_sets": ["basic", "broken-reads", "github", "github-reads", "github-writes", "packages", "pypi"]
    }


async def test_create_records_the_concrete_session_defaults_and_bootstrap(
    client: httpx.AsyncClient, custom_objects: FakeCustomObjectsApi
) -> None:
    response = await client.post(
        "/sandboxes",
        json={
            "name": "coder",
            "template": TEMPLATE,
            "policy_sets": ["packages"],
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
    assert custom_objects.objects[(SANDBOXES_PLURAL, row["name"])]["spec"]["policySets"] == ["packages"]


async def test_create_records_the_preset_it_was_made_from(
    client: httpx.AsyncClient, custom_objects: FakeCustomObjectsApi
) -> None:
    response = await client.post(
        "/sandboxes", json={"name": "from-preset", "template": TEMPLATE, "preset": "public-coder"}
    )

    assert response.status_code == 201, response.text
    assert response.json()["preset"] == "public-coder"
    assert (await client.get("/sandboxes/from-preset")).json()["preset"] == "public-coder"


async def test_create_refuses_a_preset_the_app_does_not_have(client: httpx.AsyncClient) -> None:
    response = await client.post("/sandboxes", json={"name": "no-such", "template": TEMPLATE, "preset": "missing"})

    assert response.status_code == 422


async def test_policy_sets_list_the_namespace_for_the_create_form(client: httpx.AsyncClient) -> None:
    sets = {policy_set["name"]: policy_set for policy_set in (await client.get("/action-policy/sets")).json()}
    assert {"github-reads", "github-writes", "broken-reads", "github"} <= set(sets)
    assert (sets["github-reads"]["refused"], sets["github"]["refused"]) == (None, None)
    assert sets["broken-reads"]["refused"] is not None


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
async def test_create_rejects_invalid_requests(
    client: httpx.AsyncClient, custom_objects: FakeCustomObjectsApi, body: dict
) -> None:
    response = await client.post("/sandboxes", json=body)

    assert response.status_code == 422
    assert all(kind != SANDBOXES_PLURAL or name in {"live", "fresh"} for kind, name in custom_objects.objects)


async def test_suspend_resume_apply_in_order(client: httpx.AsyncClient, custom_objects: FakeCustomObjectsApi) -> None:
    assert (await client.post("/sandboxes/live/suspend")).status_code == 204
    assert (await client.get("/sandboxes/live")).json()["operating_mode"] == "Suspended"
    assert (await client.post("/sandboxes/live/resume")).status_code == 204
    assert (await client.get("/sandboxes/live")).json()["operating_mode"] == "Running"
    assert custom_objects.objects[(SANDBOXES_PLURAL, "live")]["spec"]["running"] is True
    assert (await client.post("/sandboxes/nope/suspend")).status_code == 404


async def test_resume_during_controller_quiescing_is_retryable_in_direct_app(
    app: FastAPI, client: httpx.AsyncClient, custom_objects: FakeCustomObjectsApi, core_v1: FakeCoreV1Api
) -> None:
    stored = custom_objects.objects[(SANDBOXES_PLURAL, "live")]
    stored["spec"]["running"] = False
    stored["status"]["operatingMode"] = "Suspended"
    stored["status"]["conditions"] = [
        {
            "type": "Running",
            "status": "False",
            "reason": "Quiescing",
            "message": "waiting for the runner to seal",
            "lastTransitionTime": "2026-09-01T12:00:00Z",
        }
    ]
    app.state.services.inventory = SandboxAccess(
        SandboxInventory(namespace=NAMESPACE, custom_objects=cast(Any, custom_objects), core_v1=cast(Any, core_v1)),
        default_policy_sets=set(),
        caller=ServiceAccountRef(namespace=NAMESPACE, name="agentplane-app"),
    )
    patches = list(custom_objects.patches)

    response = await client.post("/sandboxes/live/resume")

    assert response.status_code == 503
    assert "retry resume after it is suspended" in response.json()["detail"]
    assert custom_objects.patches == patches


async def test_delete_starts_teardown_once_the_sandbox_is_suspended(
    client: httpx.AsyncClient, custom_objects: FakeCustomObjectsApi
) -> None:
    custom_objects.objects[(SANDBOXES_PLURAL, "live")]["metadata"]["finalizers"] = ["test-finalizer"]
    assert (await client.delete("/sandboxes/live")).status_code == 409
    assert (await client.get("/sandboxes/live")).json()["phase"] == "Running"

    assert (await client.post("/sandboxes/live/suspend")).status_code == 204
    assert (await client.delete("/sandboxes/live")).status_code == 204
    # The Sandbox Controller's finalizer holds it until its Sessions are sealed.
    assert (await client.get("/sandboxes/live")).json()["phase"] == "Quiescing"
    assert (await client.delete("/sandboxes/live")).status_code == 204
    assert (await client.delete("/sandboxes/nope")).status_code == 404


async def test_bound_thread_forwards_overrides_without_app_bootstrap_or_default_assembly(
    client: httpx.AsyncClient, bridge: RunnerBridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    created = (
        await client.post(
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
        )
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

    response = await client.post(
        f"/sandboxes/{created['name']}/sessions",
        json={"session_id": str(uuid4()), "spec": {"model": "thread-model", "instructions": "thread instructions"}},
    )

    assert response.status_code == 201, response.text
    assert calls == [("create", {"model": "thread-model", "instructions": "thread instructions"})]
    assert setup_scripts == [None]
    cleared = await client.post(
        f"/sandboxes/{created['name']}/sessions", json={"session_id": str(uuid4()), "spec": {}, "setup_script": ""}
    )
    assert cleared.status_code == 201, cleared.text
    assert setup_scripts == [None, ""]


@pytest.mark.parametrize("model_catalog", [CODEX_ONLY], indirect=True)
async def test_thread_resume_for_regular_sandbox_reaches_the_bridge(
    client: httpx.AsyncClient, bridge: RunnerBridge, store: ThreadStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert (await client.get("/models")).json()["harnesses"]["HARNESS_CLAUDE"] == []
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
    response = await client.post(f"/threads/{thread_id}/resume")

    assert response.status_code == 200, response.text
    assert calls == [(thread_id, Harness.CLAUDE, "/w")]


@pytest.mark.parametrize("model_catalog", [CODEX_ONLY], indirect=True)
async def test_direct_session_launch_leaves_platform_instructions_to_service(
    client: httpx.AsyncClient, bridge: RunnerBridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: list[dict[str, object]] = []

    async def create_session(
        name: str, session_id: UUID, spec: dict[str, object], setup_script: str | None = None
    ) -> runner_pb2.Attached:
        captured.append(spec)
        return runner_pb2.Attached(session_id=str(uuid4()))

    monkeypatch.setattr(bridge, "create_session", create_session)
    response = await client.post(
        "/sandboxes/live/sessions",
        json={"session_id": str(uuid4()), "spec": {"harness": "HARNESS_CLAUDE", "cwd": "/w", "model": "plain-model"}},
    )

    assert response.status_code == 201, response.text
    assert captured == [{"harness": "HARNESS_CLAUDE", "cwd": "/w", "model": "plain-model"}]


@pytest.mark.parametrize(("error_type", "status_code"), [(TimeoutError, 504), (ConnectionError, 503)])
async def test_service_launch_failure_preserves_uncertainty(
    client: httpx.AsyncClient,
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
    response = await client.post(
        "/sandboxes/live/sessions",
        json={"session_id": str(uuid4()), "spec": {"harness": "HARNESS_CLAUDE", "cwd": "/w", "model": "m"}},
    )
    assert response.status_code == status_code


async def test_create_session_answers_the_service_id(
    client: httpx.AsyncClient, bridge: RunnerBridge, monkeypatch: pytest.MonkeyPatch
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
    created = await client.post(
        path, json={"session_id": str(requested), "spec": {"cwd": "/w/{session_id}"}, "setup_script": "bootstrap"}
    )
    assert created.status_code == 201, created.text
    assert created.json()["sessionId"] == public_id
    assert received == [("live", requested, {"cwd": "/w/{session_id}"}, "bootstrap")]
    assert (await client.post(path, json={"session_id": "not-a-uuid", "spec": {}})).status_code == 422


async def test_a_runner_that_has_not_connected_is_a_503(
    tmp_path: Path,
    custom_objects: FakeCustomObjectsApi,
    core_v1: FakeCoreV1Api,
    inventory: SandboxServiceClient,
    store: ThreadStore,
    database_updates: WakeupListener,
    operator_sessions: OperatorSessionStore,
    egress: EgressAccess,
    decisions: DecisionsClient,
    live_index: LiveIndex,
    action_policy: ActionPolicyInventory,
    reviewer: TokenReviewer,
    event_logs: SeededEventLogStore,
    content: ContentStore,
    electric_proxy: ElectricProxy,
    notifications_http: httpx.AsyncClient,
    notifications_token_file: Path,
    service_history_db_url: str,
) -> None:
    """A ready Pod whose runner has not dialed the gateway yet, as right after a resume."""
    live_index.sandboxes["live"], live_index.pods["live"] = seed_runner(custom_objects, core_v1, "live")
    with backend(custom_objects, core_v1, tmp_path / "failure-token", database_url=service_history_db_url) as endpoint:
        async with dial(endpoint.target) as channel:
            app = create_app(
                inventory,
                RunnerBridge(
                    runners=RunnerDirectory(live_index, endpoint.gateway(channel)),
                    event_logs=event_logs,
                    content=content,
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
                system_status=answering_system_status(),
            )
            async with _agent_client(app) as client:
                response = await client.get("/sandboxes/live/sessions")
    assert response.status_code == 503
    assert "UNAVAILABLE" in response.json()["detail"]


@asynccontextmanager
async def unanswered_command_service(token_file: Path) -> AsyncIterator[tuple[RunnerGatewayClient, asyncio.Queue[str]]]:
    class UnansweredGateway(runner_gateway_pb2_grpc.RunnerGatewayServicer):
        @override
        async def BatchGetSessionSandboxes(
            self, request: runner_gateway_pb2.BatchGetSessionSandboxesRequest, context: grpc.aio.ServicerContext
        ) -> runner_gateway_pb2.BatchGetSessionSandboxesResponse:
            return runner_gateway_pb2.BatchGetSessionSandboxesResponse(
                sessions=[
                    runner_gateway_pb2.SessionSandbox(session_id=id, sandbox="live") for id in request.session_ids
                ]
            )

        @override
        async def SubmitCommand(
            self, request: runner_gateway_pb2.SubmitCommandRequest, context: grpc.aio.ServicerContext
        ) -> runner_gateway_pb2.SubmitCommandResponse:
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.put_nowait(request.destination.session_id)
            raise AssertionError("the command is never answered")

    cancelled: asyncio.Queue[str] = asyncio.Queue()
    server = grpc.aio.server()
    runner_gateway_pb2_grpc.add_RunnerGatewayServicer_to_server(UnansweredGateway(), server)
    await asyncio.to_thread(token_file.write_text, "test-app-command-peer")
    async with serving(server, "127.0.0.1:0", grace_s=0) as port, dial(f"127.0.0.1:{port}") as channel:
        yield (
            RunnerGatewayClient(
                channel,
                token_file=token_file,
                command_admission_timeout_s=1,
                request_timeout_s=5,
                control=SandboxControlClient(
                    control_target=f"127.0.0.1:{port}",
                    token_file=token_file,
                    request_timeout_s=5,
                    lifecycle_timeout_s=5,
                ),
            ),
            cancelled,
        )


async def test_unanswered_service_command_is_a_504_and_cancels_its_rpc(
    tmp_path: Path,
    custom_objects: FakeCustomObjectsApi,
    core_v1: FakeCoreV1Api,
    inventory: SandboxServiceClient,
    store: ThreadStore,
    database_updates: WakeupListener,
    operator_sessions: OperatorSessionStore,
    egress: EgressAccess,
    decisions: DecisionsClient,
    live_index: LiveIndex,
    action_policy: ActionPolicyInventory,
    reviewer: TokenReviewer,
    event_logs: SeededEventLogStore,
    content: ContentStore,
    electric_proxy: ElectricProxy,
    notifications_http: httpx.AsyncClient,
    notifications_token_file: Path,
) -> None:
    """Bound command admission at the public service RPC, without runner internals."""
    live_index.sandboxes["live"], live_index.pods["live"] = seed_runner(custom_objects, core_v1, "live")
    session_id = str(uuid4())
    thread_id = await event_logs.seed("live", session_id)
    async with unanswered_command_service(tmp_path / "wedged-token") as (service, cancelled):
        app = create_app(
            inventory,
            RunnerBridge(runners=RunnerDirectory(live_index, service), event_logs=event_logs, content=content),
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
            system_status=answering_system_status(),
        )
        async with _agent_client(app) as http, asyncio.timeout(10):
            response = await http.post(
                f"/threads/{thread_id}/commands",
                json={"commandId": "test-unanswered-command", "submitInput": {"text": "never admitted"}},
            )
            assert response.status_code == 504, response.text
            assert await cancelled.get() == session_id


async def test_egress_lists_the_bindings_naming_the_sandbox(client: httpx.AsyncClient) -> None:
    response = await client.get("/sandboxes/live/egress")

    assert response.status_code == 200, response.text
    body = response.json()
    assert [(b["name"], b["from_git"]) for b in body] == [("live-granted", False), ("live-seeded", True)]
    assert body[1]["policies"][0]["rules"][0]["hosts"] == ["api.github.com"]
    # Nothing names `fresh`, so nothing may leave it.
    assert (await client.get("/sandboxes/fresh/egress")).json() == []
    assert (await client.get("/sandboxes/nope/egress")).status_code == 404


@pytest.fixture
def operator_login(app: FastAPI) -> None:
    """Requests count as an operator's login session, which the routes that write bindings require."""
    app.dependency_overrides[_require_operator_login] = lambda: None


async def test_binding_writes_need_an_operator_login_not_a_workload_token(client: httpx.AsyncClient) -> None:
    for response in (
        await client.post("/sandboxes/live/policy-bindings", json={"policy_sets": ["github"]}),
        await client.delete("/policy-bindings/live-granted"),
    ):
        assert response.status_code == 403
        assert response.json() == {"detail": "operator login required"}


@pytest.mark.usefixtures("operator_login")
async def test_granting_a_running_sandbox_adds_a_binding_and_leaves_the_others(
    client: httpx.AsyncClient, custom_objects: FakeCustomObjectsApi
) -> None:
    """A sandbox's egress is not frozen at creation: the grant is one more binding naming it, and
    revoking that one leaves the rest of its egress standing."""
    granted = await client.post(
        "/sandboxes/live/policy-bindings",
        json={"policy_sets": ["pypi", "github", "github-reads"], "expires_at": "2099-01-01T00:00:00Z"},
    )

    assert granted.status_code == 201, granted.text
    binding = granted.json()
    assert (binding["subjects"], binding["from_git"]) == ([{"namespace": NAMESPACE, "name": "live"}], False)
    # Every named set is the binding's, of any rule type; only egress rules make a policy here.
    assert binding["policy_sets"] == ["pypi", "github", "github-reads"]
    assert [policy["name"] for policy in binding["policies"]] == ["pypi", "github"]
    assert custom_objects.objects[(POLICY_BINDINGS_PLURAL, binding["name"])]["spec"]["expiresAt"].startswith(
        "2099-01-01"
    )
    # Owned by the Sandbox, so deleting the sandbox takes the grant with it.
    (owner,) = custom_objects.objects[(POLICY_BINDINGS_PLURAL, binding["name"])]["metadata"]["ownerReferences"]
    assert (owner["kind"], owner["name"], owner["uid"]) == (
        "Sandbox",
        "live",
        custom_objects.objects[(SANDBOXES_PLURAL, "live")]["metadata"]["uid"],
    )
    assert sorted(b["name"] for b in (await client.get("/sandboxes/live/egress")).json()) == sorted(
        ["live-granted", "live-seeded", binding["name"]]
    )

    assert (await client.delete(f"/policy-bindings/{binding['name']}")).status_code == 204
    assert [b["name"] for b in (await client.get("/sandboxes/live/egress")).json()] == ["live-granted", "live-seeded"]


@pytest.mark.usefixtures("operator_login")
async def test_a_grant_naming_a_policy_that_does_not_exist_is_refused(
    client: httpx.AsyncClient, custom_objects: FakeCustomObjectsApi
) -> None:
    """422 and not 404: the sandbox in the path is there, and 404 on this route already says it is
    not. The CRD would admit the dangling name and the proxy would report `MissingPolicy`, so what
    the operator would otherwise get is a grant that grants nothing."""
    assert (
        await client.post("/sandboxes/live/policy-bindings", json={"policy_sets": ["github", "vanished"]})
    ).status_code == 422
    assert [b["name"] for b in (await client.get("/sandboxes/live/egress")).json()] == ["live-granted", "live-seeded"]
    assert (await client.post("/sandboxes/nope/policy-bindings", json={"policy_sets": ["github"]})).status_code == 404
    # The CRD's policies are minItems: 1, so an empty grant is refused here rather than at admission.
    assert (await client.post("/sandboxes/live/policy-bindings", json={"policy_sets": []})).status_code == 422
    # And at launch the policy sets resolve before the Sandbox exists, so a typo leaves none behind.
    assert (
        await client.post("/sandboxes", json={"name": "demo", "template": TEMPLATE, "policy_sets": ["vanished"]})
    ).status_code == 422
    assert all(kind != SANDBOXES_PLURAL or name in {"live", "fresh"} for kind, name in custom_objects.objects)


async def test_every_route_needs_one_of_the_two_credentials(client: httpx.AsyncClient) -> None:
    """Guarded from the app, not from a proxy in front of it: no header a caller sets is trusted."""
    assert (await client.get("/sandboxes", headers={"Authorization": ""})).status_code == 401
    assert (await client.get("/sandboxes", headers={"Authorization": "Bearer wrong"})).status_code == 401
    assert (await client.post("/sandboxes", json={"name": "x"}, headers={"Authorization": ""})).status_code == 401
    # A forgeable identity header (`x-authentik-username`) buys nothing.
    assert (
        await client.get("/sandboxes", headers={"Authorization": "", "x-authentik-username": "root"})
    ).status_code == 401
    assert (await client.get("/healthz", headers={"Authorization": ""})).status_code == 204


async def test_system_status_lists_each_component(client: httpx.AsyncClient) -> None:
    response = await client.get("/system/status")
    assert response.status_code == 200
    assert [(component["name"], component["state"]) for component in response.json()] == [("Test component", "ok")]
    assert (await client.get("/system/status", headers={"Authorization": ""})).status_code == 401


async def test_thread_sync_routes_authenticate_before_dispatch(client: httpx.AsyncClient) -> None:
    path = "/threads/00000000-0000-0000-0000-000000000000/sync/scope"
    assert (await client.get(path, headers={"Authorization": ""})).status_code == 401


@pytest.mark.usefixtures("operator_login")
async def test_binding_revocation(client: httpx.AsyncClient, custom_objects: FakeCustomObjectsApi) -> None:
    """A runtime binding is revoked by deleting it; one the manifest declares would be re-applied, and
    so would the Sandbox's own."""
    custom_objects.objects[(POLICY_BINDINGS_PLURAL, "live")] = policy_binding(
        "live",
        subject={"namespace": NAMESPACE, "name": "live"},
        policy_sets=["pypi"],
        from_git=False,
        sandbox_controlled="live",
    )
    assert (await client.delete("/policy-bindings/live")).status_code == 409
    assert (await client.delete("/policy-bindings/live-seeded")).status_code == 409
    assert (await client.delete("/policy-bindings/live-granted")).status_code == 204
    assert (await client.delete("/policy-bindings/live-granted")).status_code == 404
    assert [b["name"] for b in (await client.get("/sandboxes/live/egress")).json()] == ["live-seeded"]


async def test_presets_publish_editable_sandbox_and_session_defaults(client: httpx.AsyncClient) -> None:
    assert (await client.get("/presets")).json() == [
        {
            "name": "public-coder",
            "title": "Public coder",
            "template": "agentplane-test-runner",
            "policy_sets": ["basic"],
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


async def test_a_thread_is_found_by_its_session_and_renamed_in_place(
    bare_app: FastAPI, event_logs: SeededEventLogStore
) -> None:
    thread_id = str(await event_logs.seed("live", "s-1"))
    await event_logs.seed("live", "s-2")
    async with _agent_client(bare_app) as http:
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
    bare_app: FastAPI, event_logs: SeededEventLogStore
) -> None:
    thread_id = str(await event_logs.seed("live", "s-1"))
    async with _agent_client(bare_app) as http:
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
    bare_app: FastAPI,
    bridge: RunnerBridge,
    store: ThreadStore,
    event_logs: SeededEventLogStore,
    custom_objects: FakeCustomObjectsApi,
    core_v1: FakeCoreV1Api,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    custom_objects.objects[(SANDBOXES_PLURAL, "live")], core_v1.pods["live"] = seed_runner(
        custom_objects, core_v1, "live"
    )
    spec = runner_pb2.SessionSpec(harness=runner_pb2.HARNESS_CLAUDE, cwd="/w", model="test-model")
    thread_id = await event_logs.seed("live", "s-1")

    consulted: list[str] = []

    async def running_sessions(sandbox: str) -> list[runner_pb2.SessionSummary]:
        consulted.append(sandbox)
        return [
            runner_pb2.SessionSummary(
                session_id=str(thread_id), spec=spec, harness_state=runner_pb2.HARNESS_STATE_RUNNING
            )
        ]

    monkeypatch.setattr(bridge, "list_sessions", running_sessions)
    async with _agent_client(bare_app) as http:
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
async def test_notification_diagnostics_require_operator_login(client: httpx.AsyncClient, url: str) -> None:
    # Workload authentication of the app is not operator authorization to inspect another inbox.
    response = await client.get(url)
    assert response.status_code == 403
    assert response.json() == {"detail": "operator login required"}


if __name__ == "__main__":
    pytest_bazel.main()
