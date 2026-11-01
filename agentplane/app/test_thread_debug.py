"""Evidence and raw expansion use scoped, keyset reads over actual projected PostgreSQL rows."""

import httpx
import pytest_bazel

from agentplane.app.testing.history import SessionHistory
from agentplane.app.testing.replication_process import AppProcess
from agentplane.app.testing.replication_source import SANDBOX, SESSION, ReplicationSource
from agentplane.app.testing.thread_test_support import ThreadHTTP
from agentplane.history_service.testing.history import History
from agentplane.proto import command_pb2, event_pb2

# gazelle:include_dep @pypi//protobuf


async def test_lazy_scoped_evidence_and_native_expansion(
    app: AppProcess,
    store: ThreadHTTP,
    event_logs: SessionHistory,
    content: SessionHistory,
    history: History,
    agent_auth: dict[str, str],
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
    thread = await event_logs.seed(SANDBOX, SESSION)
    await history.set_attached(thread, source.attached)
    await history.record(thread, source.entries)
    scope = await content.current_scope(thread)
    assert scope is not None
    await store.wait(thread)
    path = f"/upstream/history/v1/sessions/{thread}"
    evidence_path = f"{path}/entity-evidence"
    frames_path = f"{path}/native-frames"
    params = {"projectionEpoch": scope.projection_epoch, "entity": {"entityKind": "item", "entityId": "first"}}
    async with httpx.AsyncClient(base_url=app.url) as client:
        denied = await client.post(evidence_path, json=params | {"afterCursor": "0", "limit": 2})
        assert denied.status_code == 401
        first = await client.post(evidence_path, json=params | {"afterCursor": "0", "limit": 2}, headers=agent_auth)
        assert first.status_code == 200, first.text
        assert first.json() == {
            "observations": [
                {"observationCursor": "4", "hasNative": True},
                {"observationCursor": "5", "hasNative": False},
            ],
            "nextAfterCursor": "5",
        }
        remaining = await client.post(evidence_path, json=params | {"afterCursor": "5", "limit": 2}, headers=agent_auth)
        assert remaining.json() == {"observations": [{"observationCursor": "8", "hasNative": True}]}
        # The list response contains neither semantic text nor native packet bodies.
        assert "Hello" not in first.text
        assert "second" not in remaining.text
        frames = await client.post(
            frames_path, json=params | {"observationCursor": "8", "afterSequence": "0", "limit": 2}, headers=agent_auth
        )
        assert frames.status_code == 200, frames.text
        assert frames.json()["nextAfterSequence"] == "7"
        assert [row["entry"]["event"]["native"]["line"] for row in frames.json()["frames"]] == ["second", "third"]
        missing = await client.post(
            frames_path, json=params | {"observationCursor": "8", "afterSequence": "7", "limit": 30}, headers=agent_auth
        )
        assert missing.json()["frames"] == [{"sourceSequence": str(unavailable_sequence), "entry": None}]
        assert "nextAfterSequence" not in missing.json()
        empty = await client.post(
            frames_path, json=params | {"observationCursor": "5", "afterSequence": "0", "limit": 30}, headers=agent_auth
        )
        assert empty.json()["frames"] == []
        wrong_entity = await client.post(
            frames_path,
            json=params
            | {
                "entity": {"entityKind": "item", "entityId": "second"},
                "observationCursor": "8",
                "afterSequence": "0",
                "limit": 30,
            },
            headers=agent_auth,
        )
        assert wrong_entity.status_code == 404
        stale = await client.post(
            evidence_path,
            json=params | {"projectionEpoch": "stale", "afterCursor": "0", "limit": 30},
            headers=agent_auth,
        )
        assert stale.status_code == 400
        lifecycle = await client.post(
            evidence_path,
            json=params | {"entity": {"entityKind": "lifecycle", "entityId": "2"}, "afterCursor": "0", "limit": 30},
            headers=agent_auth,
        )
        assert lifecycle.json() == {"observations": [{"observationCursor": "2", "hasNative": False}]}
        for limit in ("0", "201"):
            response = await client.post(evidence_path, json=params | {"limit": int(limit)}, headers=agent_auth)
            assert response.status_code == 400

        chronological = f"{path}/observations"
        assert (await client.get(chronological)).status_code == 401
        tail = await client.get(chronological, params={"limit": "3"}, headers=agent_auth)
        assert tail.status_code == 200, tail.text
        assert [row["cursor"] for row in tail.json()["observations"]] == ["7", "8", "9"]
        assert tail.json()["lastCursor"] == "9"
        previous = await client.get(chronological, params={"before_cursor": "7", "limit": "3"}, headers=agent_auth)
        assert [row["cursor"] for row in previous.json()["observations"]] == ["4", "5", "6"]
        assert previous.json()["lastCursor"] == "9"
        forward = await client.get(chronological, params={"after_cursor": "6", "limit": "3"}, headers=agent_auth)
        assert [row["cursor"] for row in forward.json()["observations"]] == ["7", "8", "9"]
        first_archive = await client.get(chronological, params={"after_cursor": "0", "limit": "3"}, headers=agent_auth)
        assert [row["cursor"] for row in first_archive.json()["observations"]] == ["1", "2", "3"]
        empty_archive = await client.get(chronological, params={"before_cursor": "1"}, headers=agent_auth)
        assert "observations" not in empty_archive.json() or empty_archive.json()["observations"] == []
        for bounds in (
            {"before_cursor": "7", "after_cursor": "3"},
            {"before_cursor": "-1"},
            {"before_cursor": str(2**64)},
            {"limit": "0"},
            {"limit": "201"},
        ):
            assert (await client.get(chronological, params=bounds, headers=agent_auth)).status_code == 400

        # These retained observations deliberately have no item/native-evidence association.
        # Chronological debug still reaches their complete original bodies, one expansion at a time:
        # the listing carries identity only, so opening the drawer transfers no entry at all.
        source.append(event_pb2.Event(native=event_pb2.Native(line="unlinked native packet")))
        stderr = "λ" * (1024 * 1024 + 1)
        source.append(event_pb2.Event(harness_stderr=event_pb2.HarnessStderr(text=stderr)))
        source.append(event_pb2.Event(debug_checkpoint=event_pb2.DebugCheckpoint(name="unlinked checkpoint")))
        await history.record(thread, source.entries[-3:])
        debug_tail = await client.get(chronological, params={"limit": "3"}, headers=agent_auth)
        rows = debug_tail.json()["observations"]
        assert [row["kind"] for row in rows] == ["native", "harness_stderr", "debug_checkpoint"]
        assert [row["cursor"] for row in rows] == ["10", "11", "12"]
        assert stderr not in debug_tail.text
        entries = []
        for row in rows:
            entry_page = await client.get(
                f"{path}/events", params={"after_cursor": str(int(row["cursor"]) - 1), "limit": "1"}, headers=agent_auth
            )
            assert entry_page.status_code == 200, entry_page.text
            entries.append(entry_page.json()["entries"][0])
        assert [entry["cursor"] for entry in entries] == ["10", "11", "12"]
        assert entries[0]["event"]["native"]["line"] == "unlinked native packet"
        assert entries[1]["event"]["harnessStderr"]["text"] == stderr
        assert entries[2]["event"]["debugCheckpoint"]["name"] == "unlinked checkpoint"
        absent = await client.get(f"{path}/events", params={"after_cursor": "99998", "limit": "1"}, headers=agent_auth)
        assert absent.status_code == 200
        assert not absent.json().get("entries")

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
        await history.record(thread, source.entries[-4:])
        for command_id, admission_cursor, outcome_cursor in (("failed-debug", "13", "15"), ("noop-debug", "14", "16")):
            command_scope = params | {"entity": {"entityKind": "command", "entityId": command_id}}
            command_evidence = await client.post(
                evidence_path, json=command_scope | {"afterCursor": "0", "limit": 30}, headers=agent_auth
            )
            assert [row["observationCursor"] for row in command_evidence.json()["observations"]] == [
                admission_cursor,
                outcome_cursor,
            ]
            linked = await client.post(
                frames_path,
                json=command_scope | {"observationCursor": outcome_cursor, "afterSequence": "0", "limit": 30},
                headers=agent_auth,
            )
            assert linked.status_code == 200, linked.text
            assert "entry" in linked.json()["frames"][0]


if __name__ == "__main__":
    pytest_bazel.main()
