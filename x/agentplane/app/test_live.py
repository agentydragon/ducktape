"""The pushed views against the fetched ones, the staleness verdict, and the frames a tab reads."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
import pytest_bazel
from fastapi import FastAPI
from fastapi.testclient import TestClient

from x.agentplane.app.api import Provider, create_app
from x.agentplane.app.bridge import RunnerBridge, SandboxNotReachableError
from x.agentplane.app.decisions import DecisionsClient
from x.agentplane.app.egress import EgressInventory
from x.agentplane.app.identity import TokenReviewer
from x.agentplane.app.inventory import ARCHIVED_LABEL, SANDBOXES_PLURAL, ProvisioningState, SandboxInventory
from x.agentplane.app.live import PODS_PLURAL, LiveIndex, SandboxesSnapshot, WatchHealth, frames
from x.agentplane.app.testing.kubernetes import (
    FakeCoreV1Api,
    FakeCustomObjectsApi,
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
    custom_objects.objects[(SANDBOXES_PLURAL, "shelved")] = sandbox(
        "shelved", labels={ARCHIVED_LABEL: "true"}, operating_mode="Suspended"
    )
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
    for (kind, name), obj in custom_objects.objects.items():
        match kind:
            case "sandboxes":
                live_index.sandboxes[name] = obj
            case "egressbindings":
                live_index.bindings[name] = obj
            case "egresspolicies":
                live_index.policies[name] = obj
    live_index.pods.update(core_v1.pods)
    return live_index


async def test_the_index_projects_the_rows_a_listing_would_return(
    seeded: LiveIndex, inventory: SandboxInventory
) -> None:
    """The push and the fetch share their projection; this is what says they still do."""
    assert seeded.sandbox_views(include_archived=False) == await inventory.list_sandboxes()
    assert seeded.sandbox_views(include_archived=True) == await inventory.list_sandboxes(include_archived=True)
    assert seeded.sandbox_view("runner-1") == await inventory.get("runner-1")


async def test_the_index_selects_the_bindings_a_request_would(seeded: LiveIndex, egress: EgressInventory) -> None:
    assert seeded.bindings_for("runner-1") == await egress.bindings_for("runner-1")
    assert [binding.name for binding in seeded.bindings_for("runner-1")] == ["runner-1-picked"]


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

    assert _read(await anext(stream)) == ("snapshot", ["runner-1"])
    assert _read(await anext(stream))[0] == "health"
    del seeded.sandboxes["runner-1"]
    seeded.changes.notify()

    assert await asyncio.wait_for(_next_snapshot(stream), timeout=5) == []


@pytest.fixture
def app(
    inventory: SandboxInventory,
    egress: EgressInventory,
    decisions: DecisionsClient,
    live_index: LiveIndex,
    reviewer: TokenReviewer,
) -> FastAPI:
    """Neither test below reaches a database or a runner -- the guard answers before a route body
    runs, and the document comes from the signatures -- so the engine here never connects."""

    async def unreachable(name: str) -> str:
        raise SandboxNotReachableError(name, ProvisioningState.WAITING_FOR_POD)

    store = TrajectoryStore.connect("postgresql+asyncpg://live-test@127.0.0.1:1/live-test")
    bridge = RunnerBridge(address_of=unreachable, store=store)
    return create_app(inventory, bridge, store, MODELS, egress, decisions, live_index, reviewer=reviewer)


def test_the_streams_need_a_caller(app: FastAPI) -> None:
    with TestClient(app) as unauthenticated:
        assert unauthenticated.get("/live/sandboxes").status_code == 401
        assert unauthenticated.get("/live/sandboxes/runner-1").status_code == 401


def test_the_frame_models_are_published_in_the_document(app: FastAPI) -> None:
    """The SPA's types are generated from this document, so the frames it reads have to be in it."""
    with TestClient(app) as client:
        schemas = client.get("/openapi.json").json()["components"]["schemas"]

    assert {"SandboxesSnapshot", "SandboxSnapshot", "WatchHealth"} <= set(schemas)


async def _snapshot(index: LiveIndex) -> SandboxesSnapshot:
    return SandboxesSnapshot(sandboxes=index.sandbox_views(include_archived=False), watch=index.health(NOW))


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
