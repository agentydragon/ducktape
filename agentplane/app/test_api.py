"""HTTP contract checks against the real Go app process."""

from __future__ import annotations

import errno
import socket

import httpx
import pytest
import pytest_bazel

from agentplane.app.sandbox_models import NewSandboxDefaults
from agentplane.app.testing.models import SandboxView, ThreadPage, ThreadView
from agentplane.app.testing.replication_process import AppProcess, spawn_go_app
from agentplane.sandboxes.testing.kubernetes import SandboxCluster
from util.net import listen_tcp


async def test_go_app_serves_a_parent_owned_listener(db_url: str, sandbox: SandboxCluster) -> None:
    with listen_tcp() as listener:
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


async def test_go_app_serves_sandbox_and_thread_views_using_the_shared_models(http: httpx.AsyncClient) -> None:
    sandboxes_response = await http.get("/sandboxes")
    assert sandboxes_response.status_code == 200, sandboxes_response.text
    sandboxes = [SandboxView.model_validate(row) for row in sandboxes_response.json()]
    assert [sandbox.name for sandbox in sandboxes] == ["test-runner"]
    assert sandboxes[0].operating_mode == "Running"
    assert sandboxes[0].pod is not None

    templates = await http.get("/sandboxes/templates")
    assert templates.status_code == 200, templates.text
    assert templates.json() == []

    threads_response = await http.get("/threads")
    assert threads_response.status_code == 200, threads_response.text
    page = ThreadPage.model_validate(threads_response.json())
    assert page.threads == []
    assert page.next_page_token is None


async def test_go_app_creates_a_sandbox_and_rejects_invalid_create_requests(http: httpx.AsyncClient) -> None:
    response = await http.post("/sandboxes", json={"name": "created", "template": "test-template"})
    assert response.status_code == 201, response.text
    created = SandboxView.model_validate(response.json())
    assert created.name == "created"
    assert created.operating_mode is None
    assert created.pod is None
    assert created.wake_on_send is False
    assert (await http.get("/sandboxes/created")).status_code == 200
    assert (await http.get("/sandboxes/missing")).status_code == 404

    invalid = await http.post("/sandboxes", json={"name": "missing-template"})
    assert invalid.status_code == 422


async def test_go_openapi_tracks_selected_python_http_contract_fields(http: httpx.AsyncClient) -> None:
    response = await http.get("/openapi.json")
    assert response.status_code == 200, response.text
    document = response.json()
    paths = document["paths"]
    schemas = document["components"]["schemas"]

    assert "/sandboxes" in paths
    assert "/threads" in paths
    assert "/frontend-config" in paths

    for model, schema_name in ((SandboxView, "SandboxView"), (ThreadView, "ThreadView")):
        properties = schemas[schema_name]["properties"]
        assert model.model_fields.keys() <= properties.keys()

    form_properties = schemas["SandboxFormOutputBody"]["properties"]
    assert {"policy_sets", "new_sandbox_defaults"} <= form_properties.keys()
    defaults = schemas["NewSandboxDefaults"]["properties"]
    assert NewSandboxDefaults.model_fields.keys() <= defaults.keys()


async def test_go_app_exposes_configured_browser_views_over_http(http: httpx.AsyncClient) -> None:
    frontend = await http.get("/frontend-config")
    assert frontend.status_code == 200, frontend.text
    assert frontend.json() == {"trace_search": None, "langfuse_search": None}

    presets = await http.get("/presets")
    assert presets.status_code == 200, presets.text
    assert presets.json() == []

    form = await http.get("/sandboxes/form")
    assert form.status_code == 200, form.text
    assert form.json() == {
        "policy_sets": [],
        "new_sandbox_defaults": {"wake_on_send": False, "idle_suspend_after_seconds": None},
    }


async def test_protected_go_routes_reject_requests_without_a_workload_token(app: AppProcess) -> None:
    async with httpx.AsyncClient(base_url=app.url) as anonymous:
        response = await anonymous.get("/sandboxes")

    assert response.status_code == 401


if __name__ == "__main__":
    pytest_bazel.main()
