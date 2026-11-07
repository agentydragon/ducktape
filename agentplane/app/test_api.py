"""HTTP contract checks against the real Go app process."""

from __future__ import annotations

import errno
import json
import socket

import httpx
import pytest
import pytest_bazel
from google.protobuf.json_format import MessageToDict, ParseDict

from agentplane import sandbox_resource
from agentplane.app.sandbox_models import NewSandboxDefaults
from agentplane.app.testing.replication_process import AppProcess, spawn_go_app
from agentplane.crd_group import GROUP, VERSION
from agentplane.environments.v1 import environment_service_pb2
from agentplane.proto import identity_pb2
from agentplane.sandboxes.testing.kubernetes import SandboxCluster
from util.net import listen_tcp
from util.testing.postgres import DatabaseNames, create_temporary_database

# gazelle:include_dep //agentplane/app:integration_fixtures
pytest_plugins = ("agentplane.app.integration_fixtures",)


async def test_go_app_serves_a_parent_owned_listener(
    postgres_admin_url: str, database_names: DatabaseNames, sandbox: SandboxCluster
) -> None:
    # The normal serving path must migrate a fresh database before it reports readiness.
    with create_temporary_database(postgres_admin_url, database_names()) as db_url, listen_tcp() as listener:
        config: dict[str, object] = {
            "database_url": db_url,
            "kubeconfig": str(sandbox.server.kubeconfig),
            "sandbox_namespace": sandbox.namespace,
        }
        async with spawn_go_app(config, listener=listener) as app:
            assert app.url == f"http://{listener.host}:{listener.port}"
            async with httpx.AsyncClient() as client:
                assert (await client.get(app.url + "/readyz")).status_code == 204
            with socket.socket() as competing:
                with pytest.raises(OSError, match="Address already in use") as occupied:
                    competing.bind((listener.host, listener.port))
                assert occupied.value.errno == errno.EADDRINUSE
        # The child consumes its inherited copy; the caller still owns its socket.
        assert listener.socket.fileno() >= 0
        assert listener.socket.getsockname() == (listener.host, listener.port)


async def test_go_app_serves_environment_and_thread_views_from_generated_contract(http: httpx.AsyncClient) -> None:
    environments: list[environment_service_pb2.EnvironmentDescriptor] = []
    async with http.stream(
        "GET", "/upstream/environments/v1/environments:watch", headers={"Accept": "application/x-ndjson"}
    ) as response:
        assert response.status_code == 200
        async for line in response.aiter_lines():
            if not line:
                continue
            frame = ParseDict(json.loads(line)["result"], environment_service_pb2.WatchEnvironmentsResponse())
            if frame.WhichOneof("frame") == "snapshot":
                environments.extend(frame.snapshot.environments)
            elif frame.WhichOneof("frame") == "snapshot_complete":
                break
    (target,) = [row.target for row in environments if row.target.name == "test-runner"]
    environment_response = await http.get(
        f"/upstream/environments/v1/environments/{target.namespace}/{target.name}", params={"target.uid": target.uid}
    )
    assert environment_response.status_code == 200, environment_response.text
    environment = ParseDict(environment_response.json(), environment_service_pb2.EnvironmentDescriptor())
    assert environment.target == target
    assert environment.observed_state.operating_mode == environment_service_pb2.OPERATING_MODE_RUNNING
    assert environment.kubernetes_diagnostics.HasField("pod")
    assert environment.observed_state.readiness.state == environment_service_pb2.READINESS_STATE_READY

    templates = await http.get("/upstream/environments/v1/environment-templates")
    assert templates.status_code == 200, templates.text
    assert templates.json().get("names", []) == []

    threads_response = await http.get("/upstream/app/v1/threads")
    assert threads_response.status_code == 200, threads_response.text
    assert threads_response.json().get("threads", []) == []
    assert not threads_response.json().get("nextPageToken")


async def test_go_app_creates_a_sandbox_and_rejects_invalid_create_requests(
    http: httpx.AsyncClient, sandbox: SandboxCluster
) -> None:
    response = await http.post(
        "/upstream/environments/v1/environments", json={"name": "created", "template": "test-template"}
    )
    assert response.status_code == 200, response.text
    assert response.json()["name"] == "created"
    assert response.json()["uid"]
    target = ParseDict(response.json(), identity_pb2.EnvironmentTarget())
    route = f"/upstream/environments/v1/environments/{target.namespace}/{target.name}"
    params = {"target.uid": target.uid}
    created_response = await http.get(route, params=params)
    assert created_response.status_code == 200, created_response.text
    created = ParseDict(created_response.json(), environment_service_pb2.EnvironmentDescriptor())
    assert created.target == target
    assert created.observed_state.operating_mode == environment_service_pb2.OPERATING_MODE_UNSPECIFIED
    assert not created.kubernetes_diagnostics.HasField("pod")
    assert created.configuration.wake_on_send is False
    missing = await http.get(
        f"/upstream/environments/v1/environments/{target.namespace}/missing", params={"target.uid": "missing-uid"}
    )
    assert missing.status_code == 404

    wake = await http.patch(
        f"{route}:wake-policy",
        json=MessageToDict(environment_service_pb2.SetEnvironmentWakeOnSendRequest(target=target, wake_on_send=True)),
    )
    assert wake.status_code == 200, wake.text
    assert (
        ParseDict(
            (await http.get(route, params=params)).json(), environment_service_pb2.EnvironmentDescriptor()
        ).configuration.wake_on_send
        is True
    )
    stale = await http.delete(route, params={"target.uid": "stale-uid"})
    assert stale.status_code == 404, stale.text
    assert (await http.get(route, params=params)).status_code == 200
    pending = await http.delete(route, params={"target.uid": target.uid})
    assert pending.status_code == 400, pending.text
    assert pending.json()["code"] == 9  # FailedPrecondition: controller has not reported a mode yet.
    assert (await http.get(route, params=params)).status_code == 200
    suspended = await http.post(
        f"{route}:suspend", json=MessageToDict(environment_service_pb2.EnvironmentMutationRequest(target=target))
    )
    assert suspended.status_code == 200, suspended.text
    await sandbox.custom_objects.patch_namespaced_custom_object_status(
        GROUP,
        VERSION,
        target.namespace,
        sandbox_resource.SANDBOXES_PLURAL,
        target.name,
        {"status": {"operatingMode": sandbox_resource.OperatingMode.SUSPENDED.value}},
        _content_type="application/merge-patch+json",
    )
    deleted = await http.delete(route, params={"target.uid": target.uid})
    assert deleted.status_code == 200, deleted.text
    assert (await http.get(route, params=params)).status_code == 404

    invalid = await http.post("/upstream/environments/v1/environments", json={"name": "missing-template"})
    assert invalid.status_code == 400


async def test_policy_mutations_require_the_generated_operator_facade(
    app: AppProcess, sandbox: SandboxCluster, agent_auth: dict[str, str]
) -> None:
    async with httpx.AsyncClient(
        base_url=app.url, headers={**agent_auth, "Origin": app.url}, timeout=60
    ) as workload_client:
        for method, path in [
            ("POST", f"/sandboxes/{sandbox.name}/policy-bindings"),
            ("DELETE", "/policy-bindings/operator-grant"),
        ]:
            response = await workload_client.request(method, path)
            assert response.status_code == 404, response.text

        target = f"/upstream/policy/v1/sandboxes/{sandbox.namespace}/{sandbox.name}/policy-bindings"
        grant = await workload_client.post(target, json={"sandboxUid": sandbox.uid, "policySets": ["example"]})
        assert grant.status_code == 403, grant.text

        revoke = await workload_client.delete(
            f"{target}/operator-grant", params={"sandboxUid": sandbox.uid, "bindingUid": "binding-uid"}
        )
        assert revoke.status_code == 403, revoke.text


async def test_go_openapi_tracks_selected_python_http_contract_fields(http: httpx.AsyncClient) -> None:
    response = await http.get("/openapi.json")
    assert response.status_code == 200, response.text
    document = response.json()
    paths = document["paths"]
    schemas = document["components"]["schemas"]

    assert "/frontend-config" not in paths
    assert "FrontendConfig" not in schemas

    form_properties = schemas["SandboxFormOutputBody"]["properties"]
    assert {"policy_sets", "new_sandbox_defaults"} <= form_properties.keys()
    defaults = schemas["NewSandboxDefaults"]["properties"]
    assert NewSandboxDefaults.model_fields.keys() <= defaults.keys()


async def test_go_app_exposes_configured_browser_views_over_http(http: httpx.AsyncClient) -> None:
    frontend = await http.get("/upstream/app-config/v1/frontend-config")
    assert frontend.status_code == 200, frontend.text
    # The generated gateway encodes unset message fields as ProtoJSON null; the frontend adapter maps both to null.
    assert frontend.json() == {"frontendConfig": {"traceSearch": None, "langfuseSearch": None}}

    legacy_frontend = await http.get("/frontend-config")
    assert legacy_frontend.status_code == 404, legacy_frontend.text

    models = await http.get("/upstream/app-config/v1/models")
    assert models.status_code == 200, models.text
    model_names = ["test-model-before", "test-model-after", "test-model", "test-claude-model", "test-codex-model"]
    assert models.json() == {
        "models": [
            {
                "model": name,
                "displayName": "Test " + name.removeprefix("test-").replace("-", " ").title(),
                "reasoningEfforts": ["low", "medium", "high"],
            }
            for name in model_names
        ],
        "harnesses": [{"harness": harness, "models": model_names} for harness in ("HARNESS_CLAUDE", "HARNESS_CODEX")],
    }

    presets = await http.get("/upstream/app-config/v1/presets")
    assert presets.status_code == 200, presets.text
    assert presets.json() == {"presets": []}

    form = await http.get("/sandboxes/form")
    assert form.status_code == 200, form.text
    assert form.json() == {
        "policy_sets": [],
        "new_sandbox_defaults": {"wake_on_send": False, "idle_suspend_after_seconds": None},
    }


async def test_protected_go_routes_reject_requests_without_a_workload_token(app: AppProcess) -> None:
    async with httpx.AsyncClient(base_url=app.url) as anonymous:
        response = await anonymous.get("/upstream/environments/v1/environments", params={"limit": 1})

    assert response.status_code == 401


async def test_system_status_uses_generated_appservice_facade(http: httpx.AsyncClient) -> None:
    openapi = await http.get("/openapi.json")
    assert openapi.status_code == 200, openapi.text
    assert "/system/status" not in openapi.json()["paths"]

    status = await http.get("/upstream/app/v1/system/status")
    assert status.status_code == 200, status.text
    components = status.json().get("components")
    assert isinstance(components, list)
    assert components
    assert all(
        component["state"] in {"SYSTEM_COMPONENT_STATE_OK", "SYSTEM_COMPONENT_STATE_DOWN"}
        and isinstance(component.get("checkedAt"), str)
        for component in components
    )

    legacy = await http.get("/system/status")
    assert legacy.status_code == 404, legacy.text


if __name__ == "__main__":
    pytest_bazel.main()
