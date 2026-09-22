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
from agentplane.protocol import command_pb2, event_pb2

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

        chronological = f"/threads/{thread}/conversation/observations"
        assert (await client.get(chronological)).status_code == 401
        tail = await client.get(chronological, params={"limit": "3"}, headers=AGENT_AUTH)
        assert tail.status_code == 200, tail.text
        assert [row["cursor"] for row in tail.json()["observations"]] == ["7", "8", "9"]
        assert tail.json()["next_before_cursor"] == "7"
        assert tail.json()["next_after_cursor"] is None
        previous = await client.get(chronological, params={"before_cursor": "7", "limit": "3"}, headers=AGENT_AUTH)
        assert [row["cursor"] for row in previous.json()["observations"]] == ["4", "5", "6"]
        assert previous.json()["next_before_cursor"] == "4"
        assert previous.json()["next_after_cursor"] == "6"
        forward = await client.get(chronological, params={"after_cursor": "6", "limit": "3"}, headers=AGENT_AUTH)
        assert forward.json() == tail.json()
        first_archive = await client.get(chronological, params={"after_cursor": "0", "limit": "3"}, headers=AGENT_AUTH)
        assert [row["cursor"] for row in first_archive.json()["observations"]] == ["1", "2", "3"]
        assert first_archive.json()["next_before_cursor"] is None
        assert first_archive.json()["next_after_cursor"] == "3"
        empty_archive = await client.get(chronological, params={"before_cursor": "1"}, headers=AGENT_AUTH)
        assert empty_archive.json() == {"observations": [], "next_before_cursor": None, "next_after_cursor": None}
        for bounds in (
            {"before_cursor": "7", "after_cursor": "3"},
            {"before_cursor": "-1"},
            {"before_cursor": str(2**63)},
            {"limit": "0"},
            {"limit": "201"},
        ):
            assert (await client.get(chronological, params=bounds, headers=AGENT_AUTH)).status_code == 422

        # These retained observations deliberately have no item/native-evidence association.
        # Chronological debug still reaches their complete original bodies, one expansion at a time:
        # the listing carries identity only, so opening the drawer transfers no entry at all.
        source.append(event_pb2.Event(native=event_pb2.Native(line="unlinked native packet")))
        stderr = "λ" * (1024 * 1024 + 1)
        source.append(event_pb2.Event(harness_stderr=event_pb2.HarnessStderr(text=stderr)))
        source.append(event_pb2.Event(debug_checkpoint=event_pb2.DebugCheckpoint(name="unlinked checkpoint")))
        await store.record(thread, source.entries[-3:], lease=lease)
        debug_tail = await client.get(chronological, params={"limit": "3"}, headers=AGENT_AUTH)
        rows = debug_tail.json()["observations"]
        assert [row["kind"] for row in rows] == ["native", "harness_stderr", "debug_checkpoint"]
        assert [row["cursor"] for row in rows] == ["10", "11", "12"]
        assert [row["source_sequence"] for row in rows] == ["10", "11", "12"]
        assert all(row["source_id"] == scope.source_id for row in rows)
        assert "entry" not in rows[0]
        assert stderr not in debug_tail.text
        entries = [(await client.get(f"{chronological}/{row['cursor']}", headers=AGENT_AUTH)).json() for row in rows]
        assert [entry["cursor"] for entry in entries] == ["10", "11", "12"]
        assert entries[0]["entry"]["event"]["native"]["line"] == "unlinked native packet"
        assert entries[1]["entry"]["event"]["harnessStderr"]["text"] == stderr
        assert entries[2]["entry"]["event"]["debugCheckpoint"]["name"] == "unlinked checkpoint"
        absent = await client.get(f"{chronological}/99999", headers=AGENT_AUTH)
        assert absent.status_code == 404

        for command_id in ("failed-debug", "noop-debug"):
            source.append(
                event_pb2.Event(
                    command_admitted=event_pb2.CommandAdmitted(
                        command=command_pb2.Command(
                            command_id=command_id, submit_input=command_pb2.SubmitInput(text="debug")
                        )
                    )
                )
            )
        source.append(
            event_pb2.Event(
                command_failed=event_pb2.CommandFailed(command_id="failed-debug", reason="rejected"),
                source_sequences=[10],
            )
        )
        source.append(
            event_pb2.Event(
                command_noop=event_pb2.CommandNoop(command_id="noop-debug", reason="unchanged"), source_sequences=[6]
            )
        )
        await store.record(thread, source.entries[-4:], lease=lease)
        for command_id, admission_cursor, outcome_cursor in (("failed-debug", "13", "15"), ("noop-debug", "14", "16")):
            command_scope = params | {"entity_kind": "command", "entity_id": command_id}
            command_evidence = await client.get(path, params=command_scope, headers=AGENT_AUTH)
            assert command_evidence.json() == {
                "observations": [
                    {"observation_cursor": admission_cursor, "has_native": False},
                    {"observation_cursor": outcome_cursor, "has_native": True},
                ],
                "next_after_cursor": None,
            }
            linked = await client.get(f"{path}/{outcome_cursor}/frames", params=command_scope, headers=AGENT_AUTH)
            assert linked.status_code == 200, linked.text
            assert linked.json()["frames"][0]["availability"] == "present"


if __name__ == "__main__":
    pytest_bazel.main()
