"""What recording a batch writes: the fold's entity rows, their payload bodies, and the evidence
linking an observation to the rows it touched."""

from __future__ import annotations

import pytest
import pytest_bazel
from sqlalchemy import select

from agentplane.app.agent_runtime.events.event_log import EventLogStore
from agentplane.app.agent_runtime.events.ingestion_lease import IngestionLease
from agentplane.app.agent_runtime.ingestion import Ingestion
from agentplane.app.agent_runtime.models import (
    ThreadCheckpoint,
    ThreadEntity,
    ThreadEvidence,
    ThreadNativeLink,
    ThreadPayloadChunk,
    ThreadPayloadManifest,
)
from agentplane.app.agent_runtime.thread.store import ThreadStore
from agentplane.app.conftest import SPEC, Replica, event_entry
from agentplane.app.thread.recording import ThreadFoldError
from agentplane.app.thread.views import EntityKind, ThreadOperationalState
from agentplane.protocol import command_pb2, event_pb2
from agentplane.runner import protocol_pb2

# The generated protocol stubs' own stub chain, which the mypy aspect resolves for direct deps only.
# gazelle:include_dep @pypi//protobuf


async def test_feed_failure_is_a_synced_operational_state_without_advancing_the_projection(
    event_logs: EventLogStore, ingestion: Ingestion, replica: Replica, lease: IngestionLease
) -> None:
    thread = await event_logs.open("sb-1", "s-operational", SPEC)
    attached = protocol_pb2.Attached(session_id="s-operational", spec=SPEC)
    await ingestion.set_attached(thread, attached, lease=lease)
    await ingestion.record(thread, [event_entry(1, harness_started=event_pb2.HarnessStarted())], lease=lease)
    async with replica.store._sessions() as session:
        checkpoint_before = await session.get(ThreadCheckpoint, thread)
        assert checkpoint_before is not None
        view_before = await session.get(
            ThreadEntity, (thread, checkpoint_before.projection_epoch, "view_state", "current")
        )
        assert view_before is not None
        semantic_revision = (view_before.cursor, view_before.revision_cursor)

    await ingestion.end_feed(thread, lease=lease, error="expected runner cursor 2, received 3", error_cursor=3)
    async with replica.store._sessions() as session:
        checkpoint_after = await session.get(ThreadCheckpoint, thread)
        assert checkpoint_after is not None
        view_after = await session.get(
            ThreadEntity, (thread, checkpoint_after.projection_epoch, "view_state", "current")
        )
        assert view_after is not None
        operational = ThreadOperationalState.model_validate(view_after.state["operational"])
    assert checkpoint_after.through_cursor == checkpoint_before.through_cursor == 1
    assert (view_after.cursor, view_after.revision_cursor) == semantic_revision == (1, 1)
    assert operational.model_dump() == {
        "status": "failed",
        "last_verified_cursor": "1",
        "feed_error": {"cursor": "3", "message": "expected runner cursor 2, received 3"},
    }

    await ingestion.set_attached(
        thread, protocol_pb2.Attached(session_id="s-operational", spec=SPEC, last_cursor=1), lease=lease
    )
    async with replica.store._sessions() as session:
        view_reset = await session.get(
            ThreadEntity, (thread, checkpoint_after.projection_epoch, "view_state", "current")
        )
        assert view_reset is not None
        reset = ThreadOperationalState.model_validate(view_reset.state["operational"])
    assert (view_reset.cursor, view_reset.revision_cursor) == semantic_revision
    assert reset.model_dump() == {"status": "active", "last_verified_cursor": "1", "feed_error": None}

    await ingestion.end_feed(thread, lease=lease, error="projection invariant failed")
    async with replica.store._sessions() as session:
        unknown_failure = await session.get(
            ThreadEntity, (thread, checkpoint_after.projection_epoch, "view_state", "current")
        )
        assert unknown_failure is not None
        operational = ThreadOperationalState.model_validate(unknown_failure.state["operational"])
    assert operational.model_dump() == {
        "status": "failed",
        "last_verified_cursor": "1",
        "feed_error": {"cursor": None, "message": "projection invariant failed"},
    }


async def test_record_materializes_exact_payload_revisions_and_rolls_back_unknown_observations(
    store: ThreadStore, event_logs: EventLogStore, ingestion: Ingestion, lease: IngestionLease
) -> None:
    thread = await event_logs.open("sb-1", "s-1", SPEC)
    first = event_entry(1, text_delta=event_pb2.TextDelta(item_id="old", text="hello"))
    first.event.source_sequences.append(9007)
    await ingestion.record(
        thread, [first, event_entry(2, text_delta=event_pb2.TextDelta(item_id="old", text=" world"))], lease=lease
    )
    await ingestion.record(
        thread, [event_entry(3, text_delta=event_pb2.TextDelta(item_id="old", text="!"))], lease=lease
    )
    async with store._sessions() as session:
        item = await session.scalar(
            select(ThreadEntity).where(
                ThreadEntity.thread_id == thread, ThreadEntity.entity_kind == "item", ThreadEntity.entity_id == "old"
            )
        )
        manifests = (
            await session.scalars(
                select(ThreadPayloadManifest)
                .where(ThreadPayloadManifest.thread_id == thread)
                .order_by(ThreadPayloadManifest.revision_cursor)
            )
        ).all()
        chunks = (
            await session.scalars(
                select(ThreadPayloadChunk)
                .where(ThreadPayloadChunk.thread_id == thread)
                .order_by(ThreadPayloadChunk.chunk_index)
            )
        ).all()
        evidence = (await session.scalars(select(ThreadEvidence).where(ThreadEvidence.thread_id == thread))).all()
        native_links = (
            await session.scalars(select(ThreadNativeLink).where(ThreadNativeLink.thread_id == thread))
        ).all()
    assert item is not None
    assert item.text_ref == {
        "projection_epoch": "v1",
        "owner_cursor": "1",
        "owner_id": "old",
        "field": "text",
        "revision_cursor": "3",
        "generation": "1",
    }
    assert [(manifest.revision_cursor, manifest.generation, manifest.chunk_count) for manifest in manifests] == [
        (2, 1, 1),
        (3, 1, 2),
    ]
    assert [chunk.text for chunk in chunks] == ["hello world", "!"]
    assert {(row.entity_cursor, row.observation_cursor) for row in evidence} == {(1, 1), (1, 2), (1, 3)}
    assert [(row.entity_cursor, row.observation_cursor, row.source_sequence) for row in native_links] == [(1, 1, 9007)]

    await ingestion.record(
        thread, [event_entry(4, item_completed=event_pb2.ItemCompleted(item_id="old", text=""))], lease=lease
    )
    async with store._sessions() as session:
        replacement = await session.scalar(
            select(ThreadPayloadManifest).where(
                ThreadPayloadManifest.thread_id == thread, ThreadPayloadManifest.revision_cursor == 4
            )
        )
        item = await session.scalar(
            select(ThreadEntity).where(
                ThreadEntity.thread_id == thread, ThreadEntity.entity_kind == "item", ThreadEntity.entity_id == "old"
            )
        )
    assert replacement is not None
    assert replacement.chunk_count == replacement.content_bytes == 0
    assert item is not None
    assert item.text_ref is not None
    assert item.text_ref["generation"] == item.text_ref["revision_cursor"] == "4"

    with pytest.raises(ThreadFoldError, match="cursor 5"):
        await ingestion.record(thread, [event_entry(5)], lease=lease)
    assert await event_logs.last_cursor(thread) == 4


async def test_record_projects_confirmed_input_and_parallel_tool_revisions(
    store: ThreadStore, event_logs: EventLogStore, ingestion: Ingestion, lease: IngestionLease
) -> None:
    thread = await event_logs.open("sb-1", "s-1", SPEC)
    command = command_pb2.Command(command_id="input", submit_input=command_pb2.SubmitInput(text="question"))
    confirmed = event_entry(
        2,
        harness_user_message_confirmed=event_pb2.HarnessUserMessageConfirmed(
            harness_message_id="message", text="question", origin_command_ids=["input"], turn_id="turn"
        ),
    )
    confirmed.event.source_sequences.extend([81, 82])
    await ingestion.record(
        thread,
        [
            event_entry(1, command_admitted=event_pb2.CommandAdmitted(command=command)),
            confirmed,
            event_entry(3, tool_arguments_delta=event_pb2.ToolArgumentsDelta(item_id="tool-a", partial_json="{")),
        ],
        lease=lease,
    )
    await ingestion.record(
        thread,
        [
            event_entry(
                4,
                item_started=event_pb2.ItemStarted(
                    item_id="tool-b", kind=event_pb2.ITEM_KIND_TOOL_CALL, tool_name="later"
                ),
            ),
            event_entry(
                5,
                item_completed=event_pb2.ItemCompleted(
                    item_id="tool-b", tool=event_pb2.ToolResult(output="B", succeeded=True)
                ),
            ),
            event_entry(6, tool_arguments=event_pb2.ToolArguments(item_id="tool-a", arguments_json='{"path":"x"}')),
            event_entry(
                7,
                item_completed=event_pb2.ItemCompleted(
                    item_id="tool-a", tool=event_pb2.ToolResult(output="", succeeded=False)
                ),
            ),
        ],
        lease=lease,
    )
    async with store._sessions() as session:
        entities = {
            (row.entity_kind, row.entity_id): row
            for row in (await session.scalars(select(ThreadEntity).where(ThreadEntity.thread_id == thread))).all()
        }
        manifests = (
            await session.scalars(
                select(ThreadPayloadManifest)
                .where(ThreadPayloadManifest.thread_id == thread)
                .order_by(ThreadPayloadManifest.owner_id, ThreadPayloadManifest.revision_cursor)
            )
        ).all()
    first, second = entities[("item", "tool-a")], entities[("item", "tool-b")]
    assert (first.cursor, first.revision_cursor, second.cursor, second.revision_cursor) == (3, 7, 4, 5)
    assert first.arguments_ref is not None
    assert first.arguments_ref["revision_cursor"] == "6"
    assert first.output_ref is not None
    assert first.output_ref["revision_cursor"] == "7"
    assert first.output_ref["generation"] == "7"
    assert second.output_ref is not None
    assert second.output_ref["revision_cursor"] == "5"
    assert entities[("confirmed_input", "2")].input_ref is not None
    assert entities[("command", "input")].pending is False
    assert entities[("command", "input")].state["outcome"] == "effected"
    assert [(manifest.owner_id, manifest.revision_cursor, manifest.chunk_count) for manifest in manifests] == [
        ("input", 1, 1),
        ("message", 2, 1),
        ("tool-a", 3, 1),
        ("tool-a", 6, 1),
        ("tool-a", 7, 0),
        ("tool-b", 5, 1),
    ]


async def test_a_later_batch_touching_a_completed_item_keeps_its_completion(
    store: ThreadStore, event_logs: EventLogStore, ingestion: Ingestion, lease: IngestionLease
) -> None:
    thread = await event_logs.open("sb-1", "s-1", SPEC)
    await ingestion.record(
        thread,
        [
            event_entry(1, item_completed=event_pb2.ItemCompleted(item_id="answer", text="done")),
            event_entry(
                2,
                item_completed=event_pb2.ItemCompleted(
                    item_id="tool", tool=event_pb2.ToolResult(output="out", succeeded=False)
                ),
            ),
        ],
        lease=lease,
    )
    await ingestion.record(
        thread,
        [
            event_entry(3, text_delta=event_pb2.TextDelta(item_id="answer", text="late")),
            event_entry(4, tool_output_delta=event_pb2.ToolOutputDelta(item_id="tool", text="late")),
        ],
        lease=lease,
    )
    async with store._sessions() as session:
        rows = await session.scalars(
            select(ThreadEntity).where(ThreadEntity.thread_id == thread, ThreadEntity.entity_kind == EntityKind.ITEM)
        )
        completions = {
            row.entity_id: (row.revision_cursor, row.state["completion"], row.state["tool_succeeded"]) for row in rows
        }
    assert completions == {"answer": (3, "text", None), "tool": (4, "tool", False)}


async def test_every_row_is_numbered_densely_in_thread_order_and_never_renumbered(
    store: ThreadStore, event_logs: EventLogStore, ingestion: Ingestion, lease: IngestionLease
) -> None:
    """The index is a position in the thread, so it is dense, ordered and fixed once given."""
    thread = await event_logs.open("sb-1", "s-1", SPEC)
    await ingestion.record(
        thread,
        [
            event_entry(1, harness_started=event_pb2.HarnessStarted(pid=1)),
            event_entry(2, text_delta=event_pb2.TextDelta(item_id="first", text="a")),
            event_entry(3, text_delta=event_pb2.TextDelta(item_id="second", text="b")),
        ],
        lease=lease,
    )

    async def numbered() -> list[tuple[str, str, int]]:
        async with store._sessions() as session:
            rows = (
                await session.scalars(
                    select(ThreadEntity).where(ThreadEntity.thread_id == thread).order_by(ThreadEntity.entity_index)
                )
            ).all()
            return [(row.entity_kind, row.entity_id, row.entity_index) for row in rows]

    first_pass = await numbered()
    # Dense from zero over every kind, the view state included -- a range of the index is every row
    # in that stretch of the thread, not only the rendered ones.
    assert [index for _, _, index in first_pass] == list(range(len(first_pass)))
    assert ("view_state", "current") in [(kind, entity_id) for kind, entity_id, _ in first_pass]
    assert [entity_id for kind, entity_id, _ in first_pass if kind == "item"] == ["first", "second"]

    # A revision keeps its number; a new row takes the next one.
    await ingestion.record(
        thread,
        [
            event_entry(4, text_delta=event_pb2.TextDelta(item_id="first", text="c")),
            event_entry(5, text_delta=event_pb2.TextDelta(item_id="third", text="d")),
        ],
        lease=lease,
    )
    after = await numbered()
    positions = {(kind, entity_id): index for kind, entity_id, index in after}
    before = {(kind, entity_id): index for kind, entity_id, index in first_pass}
    assert before.items() <= positions.items()
    assert [index for _, _, index in after] == list(range(len(after)))
    assert positions[("item", "third")] == len(first_pass)


if __name__ == "__main__":
    pytest_bazel.main()
