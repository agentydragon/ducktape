"""Evidence and raw expansion use scoped, keyset reads over actual projected PostgreSQL rows."""

from datetime import timedelta

import httpx
import pytest_bazel

from agentplane.app.action_policy import ActionPolicyInventory
from agentplane.app.api import create_app
from agentplane.app.bridge import RunnerBridge
from agentplane.app.conftest import AGENT_AUTH
from agentplane.app.decisions import DecisionsClient
from agentplane.app.egress import EgressInventory
from agentplane.app.identity import TokenReviewer
from agentplane.app.inventory import SandboxInventory
from agentplane.app.live import LiveIndex
from agentplane.app.presets import Harness
from agentplane.app.testing.replication_source import SANDBOX, SESSION, ReplicationSource
from agentplane.app.trajectory import TrajectoryStore
from agentplane.protocol import event_pb2

# gazelle:include_dep @pypi//protobuf


async def test_lazy_scoped_evidence_and_native_expansion(
    inventory: SandboxInventory,
    bridge: RunnerBridge,
    store: TrajectoryStore,
    egress: EgressInventory,
    decisions: DecisionsClient,
    live_index: LiveIndex,
    action_policy: ActionPolicyInventory,
    reviewer: TokenReviewer,
) -> None:
    source = ReplicationSource()
    source.append(event_pb2.Event(harness_started=event_pb2.HarnessStarted(pid=123)))
    source.append(event_pb2.Event(turn_started=event_pb2.TurnStarted(turn_id="turn", model="test-model")))
    source.append(event_pb2.Event(native=event_pb2.Native(direction=event_pb2.DIRECTION_FROM_HARNESS, line="first")))
    source.append(
        event_pb2.Event(
            item_started=event_pb2.ItemStarted(item_id="first", kind=event_pb2.ITEM_KIND_ASSISTANT_TEXT),
            source_sequences=[3],
        )
    )
    source.append(event_pb2.Event(text_delta=event_pb2.TextDelta(item_id="first", text="Hello")))
    source.append(event_pb2.Event(native=event_pb2.Native(direction=event_pb2.DIRECTION_FROM_HARNESS, line="second")))
    source.append(event_pb2.Event(native=event_pb2.Native(direction=event_pb2.DIRECTION_FROM_HARNESS, line="third")))
    unavailable_sequence = 2**53 + 9
    source.append(
        event_pb2.Event(
            text_delta=event_pb2.TextDelta(item_id="first", text=" world"),
            source_sequences=[6, 7, unavailable_sequence],
        )
    )
    source.append(
        event_pb2.Event(item_started=event_pb2.ItemStarted(item_id="second", kind=event_pb2.ITEM_KIND_ASSISTANT_TEXT))
    )
    thread = await store.thread(SANDBOX, SESSION, source.attached.spec)
    lease = await store.acquire_ingestion(SANDBOX, timedelta(minutes=1))
    assert lease is not None
    await store.set_attached(thread, source.attached, lease=lease)
    await store.record(thread, source.entries, lease=lease)
    scope = await store.current_conversation_scope(thread)
    assert scope is not None
    app = create_app(
        inventory,
        bridge,
        store,
        {harness: ["test-model"] for harness in Harness},
        egress,
        decisions,
        live_index,
        action_policy,
        reviewer=reviewer,
    )
    path = f"/threads/{thread}/conversation/evidence"
    params = {
        "source_id": scope.source_id,
        "projection_epoch": scope.projection_epoch,
        "entity_kind": "item",
        "entity_id": "first",
    }
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        denied = await client.get(path, params=params)
        assert denied.status_code == 401
        first = await client.get(path, params=params | {"limit": "2"}, headers=AGENT_AUTH)
        assert first.status_code == 200, first.text
        assert first.json() == {
            "observations": [
                {"observation_cursor": "4", "has_native": True},
                {"observation_cursor": "5", "has_native": False},
            ],
            "next_after_cursor": "5",
        }
        remaining = await client.get(path, params=params | {"after_cursor": "5", "limit": "2"}, headers=AGENT_AUTH)
        assert remaining.json() == {
            "observations": [{"observation_cursor": "8", "has_native": True}],
            "next_after_cursor": None,
        }
        # The list response contains neither semantic text nor native packet bodies.
        assert "Hello" not in first.text
        assert "second" not in remaining.text
        frames = await client.get(f"{path}/8/frames", params=params | {"limit": "2"}, headers=AGENT_AUTH)
        assert frames.status_code == 200, frames.text
        assert frames.json()["next_after_sequence"] == "7"
        assert [row["entry"]["event"]["native"]["line"] for row in frames.json()["frames"]] == ["second", "third"]
        assert all(row["availability"] == "present" for row in frames.json()["frames"])
        missing = await client.get(f"{path}/8/frames", params=params | {"after_sequence": "7"}, headers=AGENT_AUTH)
        assert missing.json() == {
            "frames": [{"source_sequence": str(unavailable_sequence), "availability": "unavailable", "entry": None}],
            "next_after_sequence": None,
        }
        empty = await client.get(f"{path}/5/frames", params=params, headers=AGENT_AUTH)
        assert empty.json() == {"frames": [], "next_after_sequence": None}
        wrong_entity = await client.get(f"{path}/8/frames", params=params | {"entity_id": "second"}, headers=AGENT_AUTH)
        assert wrong_entity.status_code == 404
        for field in ("source_id", "projection_epoch"):
            stale = await client.get(path, params=params | {field: "stale"}, headers=AGENT_AUTH)
            assert stale.status_code == 410
        lifecycle = await client.get(
            path, params=params | {"entity_kind": "lifecycle", "entity_id": "2"}, headers=AGENT_AUTH
        )
        assert lifecycle.json() == {
            "observations": [{"observation_cursor": "2", "has_native": False}],
            "next_after_cursor": None,
        }
        for limit in ("0", "201"):
            assert (await client.get(path, params=params | {"limit": limit}, headers=AGENT_AUTH)).status_code == 422


if __name__ == "__main__":
    pytest_bazel.main()
