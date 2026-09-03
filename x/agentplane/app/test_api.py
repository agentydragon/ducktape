"""The HTTP contract over the inventory: status codes, bodies, and the schema's own validation."""

from __future__ import annotations

import socket
from collections.abc import Iterator

import httpx
import pytest
import pytest_bazel
from fastapi.testclient import TestClient

from x.agentplane.app.api import create_app
from x.agentplane.app.bridge import RunnerBridge
from x.agentplane.app.inventory import ARCHIVED_LABEL, Provider, SandboxInventory
from x.agentplane.app.testing.kubernetes import FakeCoreV1Api, FakeCustomObjectsApi, pod, sandbox
from x.agentplane.app.trajectory import TrajectoryStore
from x.agentplane.runner import protocol_pb2 as pb

# TestClient drives the app over httpx, imported inside starlette; gazelle cannot see it.
# gazelle:include_dep @pypi//httpx
# The generated protocol stubs' own stub chain, which the mypy aspect resolves for direct deps only.
# gazelle:include_dep @pypi//protobuf


TEST_MODELS = {Provider.CLAUDE: ["test-claude-model"], Provider.CODEX: ["test-codex-model"]}


@pytest.fixture
def client(
    inventory: SandboxInventory,
    bridge: RunnerBridge,
    store: TrajectoryStore,
    custom_objects: FakeCustomObjectsApi,
    core_v1: FakeCoreV1Api,
) -> Iterator[TestClient]:
    custom_objects.objects[("sandboxes", "live")] = sandbox("live")
    core_v1.pods["live"] = pod("live", phase="Running", ready=True, ip="10.0.0.7")
    custom_objects.objects[("sandboxes", "fresh")] = sandbox("fresh")
    custom_objects.objects[("sandboxes", "shelved")] = sandbox(
        "shelved", labels={ARCHIVED_LABEL: "true"}, operating_mode="Suspended"
    )
    with TestClient(create_app(inventory, bridge, store, TEST_MODELS)) as test_client:
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

    assert (row["state"], row["pod"]["ip"], row["provider"]) == ("running", "10.0.0.7", "claude")
    assert row["pod"]["containers"][0]["state"] == "running"
    assert client.get("/sandboxes/nope").status_code == 404


def test_create_returns_the_new_row(client: TestClient, custom_objects: FakeCustomObjectsApi) -> None:
    response = client.post("/sandboxes", json={"slug": "demo", "provider": "codex"})

    assert response.status_code == 201
    row = response.json()
    assert row["name"].startswith("demo-")
    assert (row["state"], row["provider"]) == ("waiting_for_pod", "codex")
    assert ("sandboxes", row["name"]) in custom_objects.objects


@pytest.mark.parametrize(
    "body",
    [
        {"slug": "demo", "provider": "gemini"},
        {"slug": "Demo", "provider": "claude"},
        {"slug": "-demo", "provider": "claude"},
        {"slug": "a" * 58, "provider": "claude"},
        {"slug": "demo", "provider": "claude", "model": "cheap"},
    ],
    ids=[
        "unknown-provider",
        "uppercase-slug",
        "leading-dash-slug",
        "slug-too-long-for-a-dns-label",
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


def test_delete_removes_the_sandbox(client: TestClient, custom_objects: FakeCustomObjectsApi) -> None:
    assert client.delete("/sandboxes/live").status_code == 204
    assert ("sandboxes", "live") not in custom_objects.objects
    assert client.delete("/sandboxes/live").status_code == 404


def test_a_runner_that_does_not_answer_is_a_503(
    inventory: SandboxInventory, store: TrajectoryStore, custom_objects: FakeCustomObjectsApi, core_v1: FakeCoreV1Api
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

        with TestClient(
            create_app(inventory, RunnerBridge(address_of=nobody_listens, store=store), store, TEST_MODELS)
        ) as client:
            response = client.get("/sandboxes/live/sessions")
    assert response.status_code == 503
    assert "not answering" in response.json()["detail"]


def test_models_lists_what_each_harness_may_run(client: TestClient) -> None:
    """The catalog the session form offers; a thread carries its model, a sandbox does not."""
    assert client.get("/models").json() == {"claude": ["test-claude-model"], "codex": ["test-codex-model"]}


async def test_a_thread_is_found_by_its_session_and_renamed_in_place(
    inventory: SandboxInventory, bridge: RunnerBridge, store: TrajectoryStore
) -> None:
    """Over ASGI on this loop, not TestClient's thread: the store's pooled asyncpg connections
    belong to the loop that opened them."""
    spec = pb.SessionSpec(provider=pb.PROVIDER_CLAUDE, cwd="/w", model="test-model")
    thread_id = str(await store.thread("live", "s-1", spec))
    await store.thread("live", "s-2", spec)
    app = create_app(inventory, bridge, store, TEST_MODELS)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as http:
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
        "/threads",
        "/threads/{thread_id}",
        "/threads/{thread_id}/events",
    }
    assert set(paths["/sandboxes"]) == {"get", "post"}
    assert set(paths["/sandboxes/{name}"]) == {"get", "delete"}
    assert set(paths["/threads/{thread_id}"]) == {"get", "patch"}


if __name__ == "__main__":
    pytest_bazel.main()
