"""The HTTP contract over the inventory: status codes, bodies, and the schema's own validation."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
import pytest_bazel
from fastapi.testclient import TestClient

from x.agentplane.app.api import create_app
from x.agentplane.app.bridge import RunnerBridge
from x.agentplane.app.inventory import ARCHIVED_LABEL, SandboxInventory
from x.agentplane.app.testing.kubernetes import FakeCoreV1Api, FakeCustomObjectsApi, claim, pod, sandbox

# TestClient drives the app over httpx, imported inside starlette; gazelle cannot see it.
# gazelle:include_dep @pypi//httpx

_READY = {"conditions": [{"type": "Ready", "status": "True"}]}


@pytest.fixture
def client(
    inventory: SandboxInventory, bridge: RunnerBridge, custom_objects: FakeCustomObjectsApi, core_v1: FakeCoreV1Api
) -> Iterator[TestClient]:
    custom_objects.objects[("sandboxclaims", "live")] = claim("live", status={**_READY, "sandbox": {"name": "sb-live"}})
    custom_objects.objects[("sandboxes", "sb-live")] = sandbox("sb-live")
    core_v1.pods["sb-live"] = pod("sb-live", phase="Running", ready=True, ip="10.0.0.7")
    custom_objects.objects[("sandboxclaims", "fresh")] = claim("fresh")
    custom_objects.objects[("sandboxclaims", "shelved")] = claim(
        "shelved", labels={ARCHIVED_LABEL: "true"}, status={**_READY, "sandbox": {"name": "sb-shelved"}}
    )
    custom_objects.objects[("sandboxes", "sb-shelved")] = sandbox("sb-shelved", operating_mode="Suspended")
    with TestClient(create_app(inventory, bridge)) as test_client:
        yield test_client


def test_list_reports_state_and_hides_archived_by_default(client: TestClient) -> None:
    response = client.get("/sandboxes")

    assert response.status_code == 200
    assert {row["name"]: row["state"] for row in response.json()} == {"live": "running", "fresh": "claim_created"}
    assert {row["name"] for row in client.get("/sandboxes", params={"include_archived": "true"}).json()} == {
        "live",
        "fresh",
        "shelved",
    }


def test_get_returns_the_row_or_404(client: TestClient) -> None:
    row = client.get("/sandboxes/live").json()

    assert (row["state"], row["pod_ip"], row["provider"]) == ("running", "10.0.0.7", "claude")
    assert client.get("/sandboxes/nope").status_code == 404


def test_create_returns_the_new_row(client: TestClient, custom_objects: FakeCustomObjectsApi) -> None:
    response = client.post("/sandboxes", json={"slug": "demo", "provider": "codex", "model": "cheap"})

    assert response.status_code == 201
    row = response.json()
    assert row["name"].startswith("demo-")
    assert (row["state"], row["provider"], row["model"]) == ("claim_created", "codex", "cheap")
    assert ("sandboxclaims", row["name"]) in custom_objects.objects


@pytest.mark.parametrize(
    "body",
    [
        {"slug": "demo", "provider": "gemini", "model": "cheap"},
        {"slug": "Demo", "provider": "claude", "model": "cheap"},
        {"slug": "-demo", "provider": "claude", "model": "cheap"},
        {"slug": "a" * 58, "provider": "claude", "model": "cheap"},
        {"slug": "demo", "provider": "claude", "model": ""},
    ],
    ids=["unknown-provider", "uppercase-slug", "leading-dash-slug", "slug-too-long-for-a-dns-label", "empty-model"],
)
def test_create_rejects_invalid_requests(client: TestClient, custom_objects: FakeCustomObjectsApi, body: dict) -> None:
    response = client.post("/sandboxes", json=body)

    assert response.status_code == 422
    assert all(kind != "sandboxclaims" or name in {"live", "fresh", "shelved"} for kind, name in custom_objects.objects)


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
    assert custom_objects.objects[("sandboxes", "sb-live")]["spec"]["operatingMode"] == "Suspended"


def test_suspend_before_provisioning_is_a_conflict(client: TestClient) -> None:
    assert client.post("/sandboxes/fresh/suspend").status_code == 409
    assert client.post("/sandboxes/nope/suspend").status_code == 404


def test_delete_removes_the_claim(client: TestClient, custom_objects: FakeCustomObjectsApi) -> None:
    assert client.delete("/sandboxes/live").status_code == 204
    assert ("sandboxclaims", "live") not in custom_objects.objects
    assert client.delete("/sandboxes/live").status_code == 404


def test_healthz_answers_outside_the_schema(client: TestClient) -> None:
    assert client.get("/healthz").status_code == 204
    assert "/healthz" not in client.get("/openapi.json").json()["paths"]


def test_openapi_schema_names_every_operation(client: TestClient) -> None:
    paths = client.get("/openapi.json").json()["paths"]

    assert set(paths) == {
        "/sandboxes",
        "/sandboxes/{name}",
        "/sandboxes/{name}/suspend",
        "/sandboxes/{name}/resume",
        "/sandboxes/{name}/archive",
        "/sandboxes/{name}/unarchive",
        "/sandboxes/{name}/sessions",
        "/sandboxes/{name}/sessions/{session_id}/events",
        "/sandboxes/{name}/sessions/{session_id}/inputs",
        "/sandboxes/{name}/sessions/{session_id}/interrupt",
        "/sandboxes/{name}/sessions/{session_id}/shutdown",
    }
    assert set(paths["/sandboxes"]) == {"get", "post"}
    assert set(paths["/sandboxes/{name}"]) == {"get", "delete"}


if __name__ == "__main__":
    pytest_bazel.main()
