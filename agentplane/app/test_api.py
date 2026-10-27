"""The HTTP contract over the inventory: status codes, bodies, and the schema's own validation."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, cast, override
from uuid import UUID, uuid4

import grpc
import httpx
import pytest
import pytest_bazel
from fastapi import FastAPI
from google.protobuf.json_format import MessageToDict
from google.protobuf.timestamp_pb2 import Timestamp
from pydantic import AnyHttpUrl

from agentplane.app.action_policy import ActionPolicyInventory
from agentplane.app.api import _require_operator_login, create_app, upstream_http_error
from agentplane.app.egress_access import EgressAccess
from agentplane.app.electric import ElectricProxy
from agentplane.app.identity import TokenReviewer
from agentplane.app.live import LiveIndex
from agentplane.app.model_catalog import ModelCatalog
from agentplane.app.notifications_operator_client import NotificationsOperatorClient
from agentplane.app.operator_sessions import OperatorSessionStore
from agentplane.app.presets import PresetCatalog, SandboxPreset, ThreadPreset
from agentplane.app.sandbox_access import SandboxAccess
from agentplane.app.sandbox_models import NewSandboxDefaults
from agentplane.app.settings import FrontendConfig, LangfuseSearchSettings, TraceSearchSettings
from agentplane.app.testing.dispatcher import served_dispatcher
from agentplane.app.testing.history import SeededEventLogStore
from agentplane.app.testing.model_test_data import TEST_MODELS
from agentplane.app.testing.system_status import answering_system_status
from agentplane.app.threads.bridge import RunnerBridge
from agentplane.app.threads.dispatcher_client import SessionDispatcherClient
from agentplane.app.threads.runner_directory import RunnerDirectory, SandboxNotReachableError
from agentplane.app.threads.store import ThreadStore
from agentplane.app.threads.view.content import ContentStore
from agentplane.egress.decisions_client import DecisionsClient
from agentplane.grpc.calls import ServiceError
from agentplane.history_service.testing.history import History
from agentplane.policy_sets import POLICY_BINDINGS_PLURAL
from agentplane.postgres.wakeups import WakeupListener
from agentplane.proto import command_pb2, event_log_pb2, event_pb2, runner_pb2
from agentplane.runner.harness import Harness
from agentplane.runner_gateway.dispatcher import session_dispatcher_pb2, session_dispatcher_pb2_grpc
from agentplane.sandbox_resource import SANDBOXES_PLURAL
from agentplane.sandboxes.testing.sandbox_namespace import READY, TEMPLATE, SandboxNamespace

# The generated protocol stubs' own stub chain, which the mypy aspect resolves for direct deps only.


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
        "test-coder": SandboxPreset(
            title="Test coder",
            template=TEMPLATE,
            policy_sets=["test-basic"],
            inbox_tags=["test-group"],
            thread_preset="test-coder-codex",
            bootstrap="mkdir -p /state/workspaces",
        )
    },
    threads={
        "test-coder-codex": ThreadPreset(
            title="Test coder / Codex",
            harness=Harness.CODEX,
            model="test-codex-model",
            reasoning_effort="medium",
            instructions="preset instructions",
            setup_script="printf 'preset setup\\n'",
        )
    },
)


@pytest.fixture
def presets() -> PresetCatalog:
    return TEST_PRESETS


@pytest.fixture
def new_sandbox_defaults() -> NewSandboxDefaults:
    return NewSandboxDefaults()


@pytest.fixture
def bare_app(
    presets: PresetCatalog,
    new_sandbox_defaults: NewSandboxDefaults,
    model_catalog: ModelCatalog,
    inventory: SandboxAccess,
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
    notifications: NotificationsOperatorClient,
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
        new_sandbox_defaults=new_sandbox_defaults,
        electric=electric_proxy,
        event_logs=event_logs,
        content=content,
        database_updates=database_updates,
        operator_sessions=operator_sessions,
        notifications=notifications,
        system_status=answering_system_status(),
    )


@pytest.fixture
async def app(bare_app: FastAPI, namespace: SandboxNamespace) -> FastAPI:
    await namespace.pod("live", phase="Running", ready=True, ip="10.0.0.7")
    await namespace.sandbox("live", conditions=READY)
    await namespace.sandbox("fresh")
    await namespace.patch_status(SANDBOXES_PLURAL, "fresh", {"operatingMode": None})
    await namespace.egress_policy_set("github", [{"hosts": ["api.github.com"], "methods": ["GET"]}])
    await namespace.egress_policy_set("pypi", [{"hosts": ["pypi.org"]}])
    await namespace.policy_binding("live-seeded", subject=namespace.account("live"), policy_sets=["github"])
    await namespace.policy_binding(
        "live-granted", subject=namespace.account("live"), policy_sets=["pypi"], from_git=False
    )
    for name in ("test-packages", "test-basic"):
        await namespace.egress_policy_set(name, [{"hosts": [f"{name}.test"]}])
    await namespace.action_policy_set(
        "github-reads", [{"type": "exact_actions", "actions": {"github": ["search_code"]}}]
    )
    await namespace.action_policy_set(
        "github-writes", [{"type": "exact_actions", "actions": {"github": ["push_files"]}}]
    )
    # The CRD admits any argument schema; its readers refuse one naming no JSON Schema type.
    await namespace.action_policy_set(
        "broken-reads",
        [{"type": "argument_schema", "actions": {"github": ["search_code"]}, "schema": {"type": "no-such-json-type"}}],
    )
    return bare_app


def _agent_client(app: FastAPI, auth: dict[str, str]) -> httpx.AsyncClient:
    """The app over ASGI on the test's own loop, where its gRPC channels and database pools were opened."""
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", headers=auth)


@pytest.fixture
async def client(app: FastAPI, agent_auth: dict[str, str]) -> AsyncIterator[httpx.AsyncClient]:
    async with _agent_client(app, agent_auth) as test_client:
        yield test_client


async def test_list_reports_separate_resource_statuses(client: httpx.AsyncClient) -> None:
    response = await client.get("/sandboxes")

    assert response.status_code == 200
    rows = {row["name"]: row for row in response.json()}
    assert rows["live"]["operating_mode"] == "Running"
    assert rows["live"]["pod"]["status"]["phase"] == "Running"
    assert rows["fresh"]["operating_mode"] is None
    assert rows["fresh"]["phase"] == "Unspecified"
    assert rows["fresh"]["pod"] is None


async def test_get_returns_the_row_or_404(client: httpx.AsyncClient) -> None:
    row = (await client.get("/sandboxes/live")).json()

    assert row["operating_mode"] == "Running"
    assert row["phase"] == "Running"
    assert row["pod"]["name"] == "live"
    assert row["pod"]["status"]["podIP"] == "10.0.0.7"
    assert row["pod"]["status"]["containerStatuses"][0]["state"] == {"running": {}}
    assert (await client.get("/sandboxes/nope")).status_code == 404


async def test_create_returns_the_new_row(client: httpx.AsyncClient, namespace: SandboxNamespace) -> None:
    response = await client.post("/sandboxes", json={"name": "demo", "template": TEMPLATE})

    assert response.status_code == 201
    row = response.json()
    assert row["name"] == "demo"
    assert row["operating_mode"] is None
    assert row["phase"] == "Unspecified"
    assert row["pod"] is None
    assert row["wake_on_send"] is False
    assert (await namespace.read(SANDBOXES_PLURAL, row["name"]))["spec"]["wakeOnSend"] is False
    # Nothing was picked, so nothing may leave it.
    assert (await client.get(f"/sandboxes/{row['name']}/egress")).json() == []


async def test_templates_list_the_concrete_choices_for_the_create_form(client: httpx.AsyncClient) -> None:
    assert (await client.get("/sandboxes/templates")).json() == [TEMPLATE]


@pytest.mark.parametrize(
    ("new_sandbox_defaults", "served_defaults"),
    [
        (NewSandboxDefaults(), {"wake_on_send": False, "idle_suspend_after_seconds": None}),
        (
            NewSandboxDefaults(wake_on_send=True, idle_suspend_after_seconds=900),
            {"wake_on_send": True, "idle_suspend_after_seconds": 900},
        ),
    ],
)
async def test_the_form_offers_the_namespace_policy_sets_and_the_configured_defaults(
    client: httpx.AsyncClient, served_defaults: dict[str, object]
) -> None:
    assert (await client.get("/sandboxes/form")).json() == {
        "policy_sets": [
            "broken-reads",
            "github",
            "github-reads",
            "github-writes",
            "pypi",
            "test-basic",
            "test-packages",
        ],
        "new_sandbox_defaults": served_defaults,
    }


async def test_frontend_config_serves_optional_browser_settings(client: httpx.AsyncClient, app: FastAPI) -> None:
    assert (await client.get("/frontend-config")).json() == {"trace_search": None, "langfuse_search": None}

    app.state.services.frontend_config = FrontendConfig(
        trace_search=TraceSearchSettings(
            grafana_base_url=AnyHttpUrl("https://grafana.test.invalid"),
            tempo_datasource_uid="tempo",
            deployment_environment="test",
        ),
        langfuse_search=LangfuseSearchSettings(
            base_url=AnyHttpUrl("https://langfuse.test.invalid"),
            project_id="test-project",
            sandbox_namespace_metadata_key="attributes.agentplane.namespace",
            sandbox_service_account_metadata_key="attributes.agentplane.service_account",
        ),
    )
    assert (await client.get("/frontend-config")).json() == {
        "trace_search": {
            "grafana_base_url": "https://grafana.test.invalid/",
            "tempo_datasource_uid": "tempo",
            "deployment_environment": "test",
        },
        "langfuse_search": {
            "base_url": "https://langfuse.test.invalid/",
            "project_id": "test-project",
            "sandbox_namespace_metadata_key": "attributes.agentplane.namespace",
            "sandbox_service_account_metadata_key": "attributes.agentplane.service_account",
        },
    }


async def test_create_records_preset_label_and_bootstrap_without_per_sandbox_session_defaults(
    client: httpx.AsyncClient, namespace: SandboxNamespace
) -> None:
    response = await client.post(
        "/sandboxes",
        json={
            "name": "coder",
            "template": TEMPLATE,
            "policy_sets": ["test-packages"],
            "inbox_tags": ["test-other-group", "test-group"],
            "preset": "test-coder",
            "bootstrap": "mkdir -p /state/workspaces",
            "wake_on_send": True,
        },
    )

    assert response.status_code == 201, response.text
    row = response.json()
    assert row["preset"] == "test-coder"
    assert row["wake_on_send"] is True
    assert row["binding"]["bootstrap"] == "mkdir -p /state/workspaces"
    assert "session_defaults" not in row["binding"]
    assert (await namespace.read(SANDBOXES_PLURAL, row["name"]))["spec"] == {
        "template": TEMPLATE,
        "running": True,
        "policySets": ["test-packages"],
        "inboxTags": ["test-group", "test-other-group"],
        "bootstrap": "mkdir -p /state/workspaces",
        "wakeOnSend": True,
        "wakeGeneration": 0,
    }


async def test_create_rejects_per_sandbox_session_defaults(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/sandboxes", json={"name": "legacy-defaults", "template": TEMPLATE, "session_defaults": {"model": "old-model"}}
    )

    assert response.status_code == 422


async def test_create_records_the_preset_it_was_made_from(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/sandboxes", json={"name": "from-preset", "template": TEMPLATE, "preset": "test-coder"}
    )

    assert response.status_code == 201, response.text
    assert response.json()["preset"] == "test-coder"
    assert (await client.get("/sandboxes/from-preset")).json()["preset"] == "test-coder"


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
    client: httpx.AsyncClient, namespace: SandboxNamespace, body: dict
) -> None:
    response = await client.post("/sandboxes", json=body)

    assert response.status_code == 422
    assert await namespace.names(SANDBOXES_PLURAL) == {"live", "fresh"}


async def test_suspend_resume_apply_in_order(client: httpx.AsyncClient, namespace: SandboxNamespace) -> None:
    assert (await client.post("/sandboxes/live/suspend")).status_code == 204
    # The desired patch is not an effective-mode report.
    assert (await client.get("/sandboxes/live")).json()["operating_mode"] == "Running"
    await namespace.patch_status(SANDBOXES_PLURAL, "live", {"operatingMode": "Suspended"})
    assert (await client.get("/sandboxes/live")).json()["operating_mode"] == "Suspended"
    assert (await client.post("/sandboxes/live/resume")).status_code == 204
    assert (await client.get("/sandboxes/live")).json()["operating_mode"] == "Suspended"
    await namespace.patch_status(SANDBOXES_PLURAL, "live", {"operatingMode": "Running"})
    assert (await client.get("/sandboxes/live")).json()["operating_mode"] == "Running"
    assert (await namespace.read(SANDBOXES_PLURAL, "live"))["spec"]["running"] is True
    assert (await client.post("/sandboxes/nope/suspend")).status_code == 404


async def test_wake_policy_changes_only_the_policy_and_checks_the_displayed_uid(
    client: httpx.AsyncClient, namespace: SandboxNamespace
) -> None:
    before = await namespace.read(SANDBOXES_PLURAL, "live")
    uid = before["metadata"]["uid"]

    response = await client.patch("/sandboxes/live/wake-policy", json={"uid": uid, "wake_on_send": True})

    assert response.status_code == 204
    assert (await namespace.read(SANDBOXES_PLURAL, "live"))["spec"] == {**before["spec"], "wakeOnSend": True}

    replacement_uid = (await namespace.read(SANDBOXES_PLURAL, "fresh"))["metadata"]["uid"]
    stale = await client.patch("/sandboxes/live/wake-policy", json={"uid": replacement_uid, "wake_on_send": False})
    assert stale.status_code == 404
    assert (await namespace.read(SANDBOXES_PLURAL, "live"))["spec"]["wakeOnSend"] is True


async def test_create_records_the_idle_suspension_time(client: httpx.AsyncClient, namespace: SandboxNamespace) -> None:
    response = await client.post(
        "/sandboxes", json={"name": "napper", "template": TEMPLATE, "idle_suspend_after_seconds": 5400}
    )

    assert response.status_code == 201, response.text
    assert response.json()["idle_suspend_after_seconds"] == 5400
    assert (await namespace.read(SANDBOXES_PLURAL, "napper"))["spec"]["idleSuspendAfter"] == "1h30m"


async def test_create_refuses_an_idle_suspension_time_under_a_minute(
    client: httpx.AsyncClient, namespace: SandboxNamespace
) -> None:
    response = await client.post(
        "/sandboxes", json={"name": "napper", "template": TEMPLATE, "idle_suspend_after_seconds": 59}
    )

    assert response.status_code == 422
    assert "napper" not in await namespace.names(SANDBOXES_PLURAL)


async def test_idle_suspension_time_is_set_and_cleared_on_the_displayed_incarnation(
    client: httpx.AsyncClient, namespace: SandboxNamespace
) -> None:
    before = await namespace.read(SANDBOXES_PLURAL, "live")
    uid = before["metadata"]["uid"]

    response = await client.patch("/sandboxes/live/idle-suspend", json={"uid": uid, "idle_suspend_after_seconds": 1800})

    assert response.status_code == 204
    assert (await namespace.read(SANDBOXES_PLURAL, "live"))["spec"] == {**before["spec"], "idleSuspendAfter": "30m"}

    replacement_uid = (await namespace.read(SANDBOXES_PLURAL, "fresh"))["metadata"]["uid"]
    stale = await client.patch(
        "/sandboxes/live/idle-suspend", json={"uid": replacement_uid, "idle_suspend_after_seconds": None}
    )
    assert stale.status_code == 404
    assert (await namespace.read(SANDBOXES_PLURAL, "live"))["spec"]["idleSuspendAfter"] == "30m"

    cleared = await client.patch("/sandboxes/live/idle-suspend", json={"uid": uid, "idle_suspend_after_seconds": None})
    assert cleared.status_code == 204
    assert "idleSuspendAfter" not in (await namespace.read(SANDBOXES_PLURAL, "live"))["spec"]
    assert (await client.get("/sandboxes/live")).json()["idle_suspend_after_seconds"] is None


async def test_the_idle_suspension_time_and_reason_are_reported_as_stored(
    client: httpx.AsyncClient, namespace: SandboxNamespace
) -> None:
    assert (await client.get("/sandboxes/live")).json()["suspended_by"] is None

    # The API server returns a `metav1.Duration` as Go writes it, not as the app wrote it.
    await namespace.patch(SANDBOXES_PLURAL, "live", {"spec": {"idleSuspendAfter": "1h30m0s"}})
    await namespace.patch_status(SANDBOXES_PLURAL, "live", {"suspendedBy": "Idle", "suspendedAtWakeGeneration": 0})
    row = (await client.get("/sandboxes/live")).json()
    assert row["idle_suspend_after_seconds"] == 5400
    assert row["suspended_by"] == "Idle"

    await namespace.patch_status(
        SANDBOXES_PLURAL, "live", {"suspendedBy": "Requested", "suspendedAtWakeGeneration": None}
    )
    assert (await client.get("/sandboxes/live")).json()["suspended_by"] == "Requested"


async def test_resume_during_controller_quiescing_is_retryable_in_direct_app(
    client: httpx.AsyncClient, namespace: SandboxNamespace
) -> None:
    await namespace.patch(SANDBOXES_PLURAL, "live", {"spec": {"running": False}})
    quiescing = await namespace.patch_status(
        SANDBOXES_PLURAL,
        "live",
        {
            "operatingMode": "Suspended",
            "conditions": [
                {
                    "type": "Running",
                    "status": "False",
                    "reason": "Quiescing",
                    "message": "waiting for the runner to seal",
                    "lastTransitionTime": "2026-09-01T12:00:00Z",
                }
            ],
        },
    )

    response = await client.post("/sandboxes/live/resume")

    assert response.status_code == 503
    assert "retry resume after it is suspended" in response.json()["detail"]
    assert (await namespace.read(SANDBOXES_PLURAL, "live"))["metadata"]["resourceVersion"] == quiescing["metadata"][
        "resourceVersion"
    ]


async def test_delete_starts_teardown_once_the_sandbox_is_suspended(
    client: httpx.AsyncClient, namespace: SandboxNamespace
) -> None:
    # A newly created Sandbox is readable, but its unreported mode is not evidence that it is suspended.
    assert (await client.delete("/sandboxes/fresh")).status_code == 409
    assert "fresh" in await namespace.names(SANDBOXES_PLURAL)

    await namespace.patch(SANDBOXES_PLURAL, "live", {"metadata": {"finalizers": ["test.invalid/seal-sessions"]}})
    assert (await client.delete("/sandboxes/live")).status_code == 409
    assert (await client.get("/sandboxes/live")).json()["phase"] == "Running"

    assert (await client.post("/sandboxes/live/suspend")).status_code == 204
    # Only a controller report, not the app's desired-state patch, proves it is suspended.
    await namespace.patch_status(SANDBOXES_PLURAL, "live", {"operatingMode": "Suspended", "incarnation": None})
    assert (await client.delete("/sandboxes/live")).status_code == 204
    # The Sandbox Controller's finalizer holds it until its Sessions are sealed.
    assert (await client.get("/sandboxes/live")).json()["phase"] == "Quiescing"
    assert (await client.delete("/sandboxes/live")).status_code == 204
    assert (await client.delete("/sandboxes/nope")).status_code == 404


async def test_session_open_resolves_preset_defaults_from_sandbox_label_and_overlays_request(
    client: httpx.AsyncClient, bridge: RunnerBridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    created = (
        await client.post(
            "/sandboxes",
            json={
                "name": "coder",
                "template": TEMPLATE,
                "preset": "test-coder",
                "bootstrap": "mkdir -p /state/workspaces",
            },
        )
    ).json()
    calls: list[tuple[str, runner_pb2.SessionSpec]] = []
    setup_scripts: list[str | None] = []

    async def create_session(
        name: str, session_id: UUID, spec: runner_pb2.SessionSpec, setup_script: str | None = None
    ) -> session_dispatcher_pb2.SessionOpening:
        calls.append(("create", spec))
        setup_scripts.append(setup_script)
        return session_dispatcher_pb2.SessionOpening(session_id=str(session_id), sandbox=name)

    monkeypatch.setattr(bridge, "create_session", create_session)

    session_id = uuid4()
    response = await client.post(
        f"/sandboxes/{created['name']}/sessions",
        json={"session_id": str(session_id), "spec": {"model": "thread-model", "instructions": "thread instructions"}},
    )

    assert response.status_code == 201, response.text
    assert len(calls) == 1
    assert calls[0][0] == "create"
    assert calls[0][1] == runner_pb2.SessionSpec(
        harness=runner_pb2.HARNESS_CODEX,
        model="thread-model",
        cwd=f"/state/workspaces/{session_id}",
        reasoning_effort="medium",
        instructions="thread instructions",
    )
    assert setup_scripts == ["printf 'preset setup\\n'"]
    second_id = uuid4()
    cleared = await client.post(
        f"/sandboxes/{created['name']}/sessions", json={"session_id": str(second_id), "spec": {}, "setup_script": ""}
    )
    assert cleared.status_code == 201, cleared.text
    assert setup_scripts == ["printf 'preset setup\\n'", ""]
    assert calls[1][1] == runner_pb2.SessionSpec(
        harness=runner_pb2.HARNESS_CODEX,
        model="test-codex-model",
        cwd=f"/state/workspaces/{second_id}",
        reasoning_effort="medium",
        instructions="preset instructions",
    )


async def test_open_session_rejects_invalid_spec_fields_before_the_bridge(
    client: httpx.AsyncClient, bridge: RunnerBridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def unexpected_open(*_args: Any, **_kwargs: Any) -> session_dispatcher_pb2.SessionOpening:
        raise AssertionError("invalid proto-JSON must not reach the bridge")

    monkeypatch.setattr(bridge, "create_session", unexpected_open)
    response = await client.post(
        "/sandboxes/live/sessions", json={"session_id": str(uuid4()), "spec": {"not_a_session_field": "value"}}
    )

    assert response.status_code == 422


async def test_session_opening_endpoint_returns_dispatcher_state(
    client: httpx.AsyncClient, bridge: RunnerBridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id = uuid4()
    opening = session_dispatcher_pb2.SessionOpening(
        session_id=str(session_id),
        sandbox="live",
        state=session_dispatcher_pb2.DELIVERY_STATE_QUEUED_IN_DISPATCHER,
        reason="sandbox is not Ready",
    )

    async def get_opening(sandbox: str, requested_id: UUID) -> session_dispatcher_pb2.SessionOpening:
        assert sandbox == "live"
        assert requested_id == session_id
        return opening

    monkeypatch.setattr(bridge, "session_opening", get_opening)
    response = await client.get(f"/sandboxes/live/sessions/{session_id}/opening")

    assert response.status_code == 200
    assert response.json() == {
        "sessionId": str(session_id),
        "sandbox": "live",
        "state": "DELIVERY_STATE_QUEUED_IN_DISPATCHER",
        "reason": "sandbox is not Ready",
    }


async def test_runner_backed_sandbox_session_inventory_route_is_removed(
    client: httpx.AsyncClient, app: FastAPI
) -> None:
    response = await client.get("/sandboxes/live/sessions")

    assert response.status_code == 405
    operations = app.openapi()["paths"]["/sandboxes/{name}/sessions"]
    assert "get" not in operations
    assert "post" in operations


@pytest.mark.parametrize("model_catalog", [CODEX_ONLY], indirect=True)
async def test_direct_session_launch_leaves_platform_instructions_to_service(
    client: httpx.AsyncClient, bridge: RunnerBridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: list[dict[str, object]] = []

    async def create_session(
        name: str, session_id: UUID, spec: runner_pb2.SessionSpec, setup_script: str | None = None
    ) -> session_dispatcher_pb2.SessionOpening:
        captured.append({"harness": runner_pb2.Harness.Name(spec.harness), "cwd": spec.cwd, "model": spec.model})
        return session_dispatcher_pb2.SessionOpening(session_id=str(session_id), sandbox=name)

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
        name: str, session_id: UUID, spec: runner_pb2.SessionSpec, setup_script: str | None = None
    ) -> session_dispatcher_pb2.SessionOpening:
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
    """The client's Session ID reaches the bridge and stays the opening's public identity."""
    requested = uuid4()
    received: list[tuple[str, UUID, runner_pb2.SessionSpec, str | None]] = []

    async def create_session(
        sandbox: str, session_id: UUID, spec: runner_pb2.SessionSpec, setup_script: str | None = None
    ) -> session_dispatcher_pb2.SessionOpening:
        received.append((sandbox, session_id, spec, setup_script))
        return session_dispatcher_pb2.SessionOpening(session_id=str(session_id), sandbox=sandbox)

    monkeypatch.setattr(bridge, "create_session", create_session)
    path = "/sandboxes/live/sessions"
    created = await client.post(
        path, json={"session_id": str(requested), "spec": {"cwd": "/w/{session_id}"}, "setup_script": "bootstrap"}
    )
    assert created.status_code == 201, created.text
    assert created.json()["sessionId"] == str(requested)
    assert received == [("live", requested, runner_pb2.SessionSpec(cwd=f"/w/{requested}"), "bootstrap")]
    assert (await client.post(path, json={"session_id": "not-a-uuid", "spec": {}})).status_code == 422


@asynccontextmanager
async def unanswered_dispatcher_service(
    token_file: Path,
) -> AsyncIterator[tuple[SessionDispatcherClient, asyncio.Queue[str]]]:
    class UnansweredDispatcher(session_dispatcher_pb2_grpc.SessionDispatcherServicer):
        @override
        async def SubmitInput(
            self, request: session_dispatcher_pb2.SubmitInputRequest, context: grpc.aio.ServicerContext
        ) -> session_dispatcher_pb2.SubmitInputResponse:
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.put_nowait(request.session_id)
            raise AssertionError("the dispatch is never answered")

    cancelled: asyncio.Queue[str] = asyncio.Queue()
    async with served_dispatcher(UnansweredDispatcher(), token_file, request_timeout_s=1) as client:
        yield (client, cancelled)


@asynccontextmanager
async def queued_dispatcher_service(token_file: Path) -> AsyncIterator[SessionDispatcherClient]:
    class QueuedDispatcher(session_dispatcher_pb2_grpc.SessionDispatcherServicer):
        @override
        async def SubmitInput(
            self, request: session_dispatcher_pb2.SubmitInputRequest, _context: grpc.aio.ServicerContext
        ) -> session_dispatcher_pb2.SubmitInputResponse:
            return session_dispatcher_pb2.SubmitInputResponse(
                dispatch=session_dispatcher_pb2.Dispatch(
                    command_id=request.command_id,
                    session_id=request.session_id,
                    sandbox=request.sandbox,
                    sandbox_uid="test-sandbox-uid",
                    operation=session_dispatcher_pb2.OPERATION_SUBMIT_INPUT,
                    state=session_dispatcher_pb2.DELIVERY_STATE_QUEUED_IN_DISPATCHER,
                    reason="sandbox_resuming",
                )
            )

    async with served_dispatcher(QueuedDispatcher(), token_file, request_timeout_s=5) as client:
        yield client


async def test_bridge_returns_the_dispatcher_queued_receipt_over_grpc(
    tmp_path: Path, bridge: RunnerBridge, event_logs: SeededEventLogStore
) -> None:
    thread_id = await event_logs.seed("live", str(uuid4()))
    command = command_pb2.Command(
        command_id="queued-at-dispatcher", submit_input=command_pb2.SubmitInput(text="wait for resume")
    )
    async with queued_dispatcher_service(tmp_path / "queued-token") as dispatcher:
        bridge.dispatcher = dispatcher
        result = (
            await bridge.dispatcher.unary(
                bridge.dispatcher.stub.SubmitInput,
                session_dispatcher_pb2.SubmitInputRequest(
                    command_id=command.command_id, session_id=str(thread_id), sandbox="live", text="wait for resume"
                ),
            )
        ).dispatch

    assert isinstance(result, session_dispatcher_pb2.Dispatch)
    assert result.command_id == command.command_id
    assert result.state == session_dispatcher_pb2.DELIVERY_STATE_QUEUED_IN_DISPATCHER
    assert result.reason == "sandbox_resuming"


async def test_command_submission_tags_dispatch_and_archived_retry_after_sandbox_deletion(
    client: httpx.AsyncClient,
    bridge: RunnerBridge,
    event_logs: SeededEventLogStore,
    history: History,
    store: ThreadStore,
    namespace: SandboxNamespace,
    tmp_path: Path,
) -> None:
    thread_id = await event_logs.seed("live", str(uuid4()))
    command = command_pb2.Command(command_id="retry-after-archive", submit_input=command_pb2.SubmitInput(text="once"))
    path = f"/threads/{thread_id}/submit-input"
    body = {"commandId": command.command_id, "text": command.submit_input.text}
    async with queued_dispatcher_service(tmp_path / "queued-token") as dispatcher:
        bridge.dispatcher = dispatcher
        first = await client.post(path, json=body)
        assert first.status_code == 200, first.text
        assert first.json()["kind"] == "dispatch"
        assert first.json()["dispatch"]["commandId"] == command.command_id

    timestamp = Timestamp()
    timestamp.GetCurrentTime()
    admission = event_log_pb2.EventEntry(
        cursor=1,
        origin=event_log_pb2.EventOrigin(source_id="test-runner", sequence=1),
        event=event_pb2.Event(at=timestamp, command_admitted=event_pb2.CommandAdmitted(command=command)),
    )
    await history.record(thread_id, [admission])
    await store.archive(thread_id)
    await namespace.delete(SANDBOXES_PLURAL, "live")

    retry = await client.post(path, json=body)
    assert retry.status_code == 200, retry.text
    assert retry.json() == {"kind": "archived_admission", "admission": MessageToDict(admission)}
    conflict = await client.post(path, json={"commandId": command.command_id, "text": "different"})
    assert conflict.status_code == 409, conflict.text


async def test_unanswered_service_command_is_a_504_and_cancels_its_rpc(
    tmp_path: Path,
    namespace: SandboxNamespace,
    agent_auth: dict[str, str],
    inventory: SandboxAccess,
    store: ThreadStore,
    database_updates: WakeupListener,
    operator_sessions: OperatorSessionStore,
    egress: EgressAccess,
    decisions: DecisionsClient,
    live_index: LiveIndex,
    runners: RunnerDirectory,
    action_policy: ActionPolicyInventory,
    reviewer: TokenReviewer,
    event_logs: SeededEventLogStore,
    content: ContentStore,
    electric_proxy: ElectricProxy,
    notifications: NotificationsOperatorClient,
) -> None:
    """Bound command admission at the public service RPC, without runner internals."""
    live_index.sandboxes["live"], live_index.pods["live"] = await namespace.runner("live")
    session_id = str(uuid4())
    thread_id = await event_logs.seed("live", session_id)
    async with unanswered_dispatcher_service(tmp_path / "wedged-token") as (dispatcher, cancelled):
        app = create_app(
            inventory,
            RunnerBridge(runners=runners, event_logs=event_logs, content=content, dispatcher=dispatcher),
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
            notifications=notifications,
            system_status=answering_system_status(),
        )
        async with _agent_client(app, agent_auth) as http, asyncio.timeout(10):
            response = await http.post(
                f"/threads/{thread_id}/submit-input",
                json={"commandId": "test-unanswered-command", "text": "never admitted"},
            )
            assert response.status_code == 504, response.text
            assert await cancelled.get() == session_id


async def test_egress_lists_the_bindings_naming_the_sandbox(client: httpx.AsyncClient) -> None:
    response = await client.get("/sandboxes/live/egress")

    assert response.status_code == 200, response.text
    body = response.json()
    assert [(b["name"], b["provenance"]) for b in body] == [("live-granted", "operator"), ("live-seeded", "git")]
    assert body[1]["policy_sets"][0]["rules"][0]["hosts"] == ["api.github.com"]
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
    client: httpx.AsyncClient, namespace: SandboxNamespace
) -> None:
    """A sandbox's egress is not frozen at creation: the grant is one more binding naming it, and
    revoking that one leaves the rest of its egress standing."""
    granted = await client.post(
        "/sandboxes/live/policy-bindings",
        json={"policy_sets": ["pypi", "github", "github-reads"], "expires_at": "2099-01-01T00:00:00Z"},
    )

    assert granted.status_code == 201, granted.text
    binding = granted.json()
    assert (binding["subjects"], binding["provenance"]) == ([namespace.account("live").model_dump()], "operator")
    # Every named set is the binding's, of any rule type.
    assert [policy_set["name"] for policy_set in binding["policy_sets"]] == ["pypi", "github", "github-reads"]
    stored = await namespace.read(POLICY_BINDINGS_PLURAL, binding["name"])
    assert stored["spec"]["expiresAt"].startswith("2099-01-01")
    # Owned by the Sandbox, so deleting the sandbox takes the grant with it.
    (owner,) = stored["metadata"]["ownerReferences"]
    assert (owner["kind"], owner["name"], owner["uid"]) == (
        "Sandbox",
        "live",
        (await namespace.read(SANDBOXES_PLURAL, "live"))["metadata"]["uid"],
    )
    assert sorted(b["name"] for b in (await client.get("/sandboxes/live/egress")).json()) == sorted(
        ["live-granted", "live-seeded", binding["name"]]
    )

    assert (await client.delete(f"/policy-bindings/{binding['name']}")).status_code == 204
    assert [b["name"] for b in (await client.get("/sandboxes/live/egress")).json()] == ["live-granted", "live-seeded"]


@pytest.mark.usefixtures("operator_login")
async def test_a_grant_naming_a_policy_that_does_not_exist_is_refused(
    client: httpx.AsyncClient, namespace: SandboxNamespace
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
    assert await namespace.names(SANDBOXES_PLURAL) == {"live", "fresh"}


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


@pytest.mark.parametrize(
    "path",
    [
        "/threads/form",
        "/threads/form/observations",
        "/threads/form/sync/scope",
        "/sandboxes/live/sessions/form/opening",
    ],
)
async def test_a_non_uuid_id_segment_matches_no_route(client: httpx.AsyncClient, path: str) -> None:
    """A removed literal route an old browser tab still calls is not found, not a malformed ID."""
    assert (await client.get(path)).status_code == 404


@pytest.mark.usefixtures("operator_login")
async def test_binding_revocation(client: httpx.AsyncClient, namespace: SandboxNamespace) -> None:
    """A runtime binding is revoked by deleting it; one the manifest declares would be re-applied, and
    so would the Sandbox's own."""
    await namespace.policy_binding(
        "live", subject=namespace.account("live"), policy_sets=["pypi"], from_git=False, sandbox_controlled="live"
    )
    assert (await client.delete("/policy-bindings/live")).status_code == 409
    assert (await client.delete("/policy-bindings/live-seeded")).status_code == 409
    assert (await client.delete("/policy-bindings/live-granted")).status_code == 204
    assert (await client.delete("/policy-bindings/live-granted")).status_code == 404
    assert [b["name"] for b in (await client.get("/sandboxes/live/egress")).json()] == ["live", "live-seeded"]


async def test_presets_publish_editable_sandbox_and_session_defaults(client: httpx.AsyncClient) -> None:
    assert (await client.get("/presets")).json() == [
        {
            "name": "test-coder",
            "title": "Test coder",
            "template": TEMPLATE,
            "policy_sets": ["test-basic"],
            "inbox_tags": ["test-group"],
            "session_defaults": {
                "harness": "HARNESS_CODEX",
                "model": "test-codex-model",
                "cwd": "/state/workspaces/{session_id}",
                "reasoning_effort": "medium",
                "instructions": "preset instructions",
                "setup_script": "printf 'preset setup\\n'",
            },
            "bootstrap": "mkdir -p /state/workspaces",
        }
    ]


async def test_a_thread_is_found_by_its_session_and_renamed_in_place(
    bare_app: FastAPI, event_logs: SeededEventLogStore, agent_auth: dict[str, str]
) -> None:
    thread_id = str(await event_logs.seed("live", "s-1"))
    await event_logs.seed("live", "s-2")
    async with _agent_client(bare_app, agent_auth) as http:
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
    bare_app: FastAPI, event_logs: SeededEventLogStore, agent_auth: dict[str, str]
) -> None:
    thread_id = str(await event_logs.seed("live", "s-1"))
    async with _agent_client(bare_app, agent_auth) as http:
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


@pytest.mark.parametrize("busy", [True, False])
async def test_archive_checks_unfinished_work_instead_of_process_liveness(
    busy: bool,
    bare_app: FastAPI,
    bridge: RunnerBridge,
    store: ThreadStore,
    event_logs: SeededEventLogStore,
    namespace: SandboxNamespace,
    agent_auth: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await namespace.runner("live")
    thread_id = await event_logs.seed("live", "s-1")
    assert event_logs.history is not None
    await event_logs.history.set_attached(
        thread_id, runner_pb2.Attached(session_id=str(thread_id), harness_state=runner_pb2.HARNESS_STATE_STOPPED)
    )
    thread = await store.get_thread(thread_id)
    assert thread is not None
    assert thread.harness_state == "HARNESS_STATE_STOPPED"

    consulted: list[str] = []

    async def runner_status(sandbox: str) -> runner_pb2.RunnerStatus:
        consulted.append(sandbox)
        return runner_pb2.RunnerStatus(sessions=[runner_pb2.SessionStatus(session_id=str(thread_id), busy=busy)])

    monkeypatch.setattr(bridge, "runner_status", runner_status)
    async with _agent_client(bare_app, agent_auth) as http:
        response = await http.post(f"/threads/{thread_id}/archive")

    assert response.status_code == (409 if busy else 204)
    assert consulted == ["live"]
    thread = await store.get_thread(thread_id)
    assert thread is not None
    assert thread.archived is not busy


async def test_a_thread_is_not_archived_when_its_live_runner_cannot_be_checked(
    bare_app: FastAPI,
    bridge: RunnerBridge,
    store: ThreadStore,
    event_logs: SeededEventLogStore,
    namespace: SandboxNamespace,
    agent_auth: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await namespace.runner("live")
    thread_id = await event_logs.seed("live", "runner-loss")

    async def runner_unavailable(_sandbox: str) -> runner_pb2.RunnerStatus:
        raise ServiceError(grpc.StatusCode.UNAVAILABLE, "runner connection unavailable")

    monkeypatch.setattr(bridge, "runner_status", runner_unavailable)
    async with _agent_client(bare_app, agent_auth) as http:
        response = await http.post(f"/threads/{thread_id}/archive")

    assert response.status_code == 503
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


async def test_runner_status_preserves_topology_and_exact_failure_cursor(
    client: httpx.AsyncClient, bridge: RunnerBridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def snapshot(sandbox: str) -> runner_pb2.RunnerStatus:
        assert sandbox == "live"
        return runner_pb2.RunnerStatus(
            runner_boot_id="runner-boot",
            processes=[
                runner_pb2.HarnessProcessStatus(
                    process_id="spawn-a",
                    pid=123,
                    harness=runner_pb2.HARNESS_CODEX,
                    state=runner_pb2.PROCESS_STATE_RUNNING,
                    assigned_session_ids=["ready", "loading"],
                )
            ],
            sessions=[
                runner_pb2.SessionStatus(
                    session_id="ready",
                    process_id="spawn-a",
                    native_session_id="native-a",
                    execution_state=runner_pb2.SESSION_EXECUTION_STATE_READY,
                    busy=False,
                ),
                runner_pb2.SessionStatus(
                    session_id="loading",
                    process_id="spawn-a",
                    execution_state=runner_pb2.SESSION_EXECUTION_STATE_PREPARING,
                    busy=True,
                ),
                runner_pb2.SessionStatus(
                    session_id="failed",
                    execution_state=runner_pb2.SESSION_EXECUTION_STATE_FAILED,
                    failure_cursor=2**63 + 1,
                    busy=False,
                ),
            ],
        )

    monkeypatch.setattr(bridge, "runner_status", snapshot)
    response = await client.get("/sandboxes/live/runner-status")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["runner_boot_id"] == "runner-boot"
    assert body["processes"][0]["assigned_session_ids"] == ["ready", "loading"]
    assert body["sessions"][0]["process_id"] == "spawn-a"
    assert body["sessions"][1]["execution_state"] == "SESSION_EXECUTION_STATE_PREPARING"
    assert body["sessions"][2]["failure_cursor"] == str(2**63 + 1)
    assert body["sessions"][2]["process_id"] is None


async def test_unreachable_runner_status_reports_unavailable(
    client: httpx.AsyncClient, bridge: RunnerBridge, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def unavailable(sandbox: str) -> runner_pb2.RunnerStatus:
        raise SandboxNotReachableError(sandbox)

    monkeypatch.setattr(bridge, "runner_status", unavailable)
    response = await client.get("/sandboxes/live/runner-status")
    assert response.status_code == 503


if __name__ == "__main__":
    pytest_bazel.main()
