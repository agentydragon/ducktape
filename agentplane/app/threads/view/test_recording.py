"""What recording a batch writes: the fold's entity rows, their payload bodies, and the evidence
linking an observation to the rows it touched."""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import cast
from uuid import UUID, uuid4

import pytest_bazel
from sqlalchemy import select

from agentplane.app.testing.history import SeededEventLogStore
from agentplane.app.testing.thread_test_support import SPEC, event_entry
from agentplane.history_service.fold.models import (
    ThreadCheckpoint,
    ThreadEntity,
    ThreadEvidence,
    ThreadNativeLink,
    ThreadPayloadChunk,
    ThreadPayloadManifest,
)
from agentplane.history_service.fold.views import EntityKind, ThreadOperationalState
from agentplane.history_service.fold_runner import FOLD_EPOCH
from agentplane.history_service.testing.history import History
from agentplane.proto import command_pb2, event_pb2, runner_pb2
from agentplane.runner.codex import CodexAdapter
from agentplane.runner.config import CodexLaunch
from agentplane.runner.harness import Harness
from agentplane.runner.observation import Observation
from agentplane.runner.session import Session
from agentplane.runner.store import SessionRecord

# The generated protocol stubs' own stub chain, which the mypy aspect resolves for direct deps only.
# gazelle:include_dep @pypi//protobuf


class _HarnessOutputRecorder:
    """The Codex adapter's session seam, recording observations before database ingestion."""

    def __init__(self) -> None:
        self.record = SessionRecord(
            harness=Harness.CODEX,
            cwd="/workspace",
            model="gpt-5.4",
            reasoning_effort="low",
            instructions="",
            event_source_id=uuid4(),
        )
        self.active_turn_id = ""
        self.observations: list[tuple[Observation, list[int]]] = []

    async def emit(self, observation: Observation, *, sources: Sequence[int]) -> None:
        self.observations.append((observation, list(sources)))


async def view_state(history: History, thread: UUID) -> tuple[ThreadCheckpoint, ThreadEntity]:
    """The thread's checkpoint and its current `view_state` row."""
    async with history.sessions() as session:
        checkpoint = await session.get(ThreadCheckpoint, thread)
        assert checkpoint is not None
        view = await session.get(ThreadEntity, (thread, checkpoint.projection_epoch, "view_state", "current"))
        assert view is not None
    return checkpoint, view


def operational_state(view: ThreadEntity) -> dict[str, object]:
    return ThreadOperationalState.model_validate(view.state["operational"]).model_dump()


async def test_feed_failure_is_a_synced_operational_state_without_advancing_the_projection(
    event_logs: SeededEventLogStore, history: History
) -> None:
    thread = await event_logs.seed("sb-1", "s-operational")
    attached = runner_pb2.Attached(session_id="s-operational", spec=SPEC)
    await history.set_attached(thread, attached)
    await history.record(thread, [event_entry(1, harness_started=event_pb2.HarnessStarted())])
    checkpoint_before, view_before = await view_state(history, thread)
    semantic_revision = (view_before.cursor, view_before.revision_cursor)

    await history.fail_feed(thread, error="expected runner cursor 2, received 3", error_cursor=3)
    checkpoint_after, view_after = await view_state(history, thread)
    assert checkpoint_after.through_cursor == checkpoint_before.through_cursor == 1
    assert (view_after.cursor, view_after.revision_cursor) == semantic_revision == (1, 1)
    assert operational_state(view_after) == {
        "status": "failed",
        "last_verified_cursor": "1",
        "feed_error": {"cursor": "3", "message": "expected runner cursor 2, received 3"},
    }

    await history.set_attached(thread, runner_pb2.Attached(session_id="s-operational", spec=SPEC, last_cursor=1))
    _, view_reset = await view_state(history, thread)
    assert (view_reset.cursor, view_reset.revision_cursor) == semantic_revision
    assert operational_state(view_reset) == {"status": "active", "last_verified_cursor": "1", "feed_error": None}

    await history.fail_feed(thread, error="projection invariant failed")
    _, unknown_failure = await view_state(history, thread)
    assert operational_state(unknown_failure) == {
        "status": "failed",
        "last_verified_cursor": "1",
        "feed_error": {"cursor": None, "message": "projection invariant failed"},
    }


async def test_codex_harness_nul_reaches_the_persisted_thread_payload(
    event_logs: SeededEventLogStore, history: History
) -> None:
    """An escaped NUL in Codex's stdout frame must survive adapter translation and thread folding."""
    thread = await event_logs.seed("sb-1", "s-codex-nul")
    output = "before\x00after"
    recorder = _HarnessOutputRecorder()
    adapter = CodexAdapter(
        cast(Session, recorder), CodexLaunch(binary=Path("/bin/false"), base_url="http://unused", api_key="unused")
    )
    harness_line = (
        r'{"method":"item/commandExecution/outputDelta",'
        r'"params":{"threadId":"codex-thread","turnId":"turn","itemId":"tool",'
        r'"delta":"before\u0000after"}}'
    )
    harness_frame = json.loads(harness_line)

    await adapter.on_frame(harness_frame, 1)

    assert recorder.observations == [(event_pb2.ToolOutputDelta(item_id="tool", text=output), [1])]
    observed_output = recorder.observations[0][0]
    assert isinstance(observed_output, event_pb2.ToolOutputDelta)
    native = event_entry(1, native=event_pb2.Native(direction=event_pb2.DIRECTION_FROM_HARNESS, line=harness_line))
    output_delta = event_entry(2, tool_output_delta=observed_output)
    output_delta.event.source_sequences.append(1)
    await history.record(
        thread, [native, output_delta, event_entry(3, turn_started=event_pb2.TurnStarted(turn_id="next"))]
    )

    entries = await event_logs.events(thread, limit=10)
    assert [entry.cursor for entry in entries] == [1, 2, 3]
    assert entries[0].event.native.line == harness_line
    assert entries[1].event.tool_output_delta.text == output
    assert entries[1].event.source_sequences == [1]
    assert await event_logs.last_cursor(thread) == 3
    async with history.sessions() as session:
        chunks = (
            await session.scalars(
                select(ThreadPayloadChunk).where(
                    ThreadPayloadChunk.thread_id == thread,
                    ThreadPayloadChunk.owner_id == "tool",
                    ThreadPayloadChunk.field == "output",
                )
            )
        ).all()
    assert [chunk.text for chunk in chunks] == [output]


async def test_record_materializes_exact_payload_revisions_and_stalls_on_unknown_observations(
    event_logs: SeededEventLogStore, history: History
) -> None:
    thread = await event_logs.seed("sb-1", "s-1")
    first = event_entry(1, text_delta=event_pb2.TextDelta(item_id="old", text="hello"))
    first.event.source_sequences.append(9007)
    await history.record(thread, [first, event_entry(2, text_delta=event_pb2.TextDelta(item_id="old", text=" world"))])
    await history.record(thread, [event_entry(3, text_delta=event_pb2.TextDelta(item_id="old", text="!"))])
    async with history.sessions() as session:
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
        "projection_epoch": FOLD_EPOCH,
        "owner_cursor": "1",
        "owner_id": "old",
        "field": "text",
        "revision_cursor": "3",
        "generation": "1",
        # The revision spans both chunks its generation has so far: what a reader may render.
        "chunk_count": "2",
    }
    assert [(manifest.revision_cursor, manifest.generation, manifest.chunk_count) for manifest in manifests] == [
        (2, 1, 1),
        (3, 1, 2),
    ]
    assert [chunk.text for chunk in chunks] == ["hello world", "!"]
    assert {(row.entity_cursor, row.observation_cursor) for row in evidence} == {(1, 1), (1, 2), (1, 3)}
    assert [(row.entity_cursor, row.observation_cursor, row.source_sequence) for row in native_links] == [(1, 1, 9007)]

    await history.record(thread, [event_entry(4, item_completed=event_pb2.ItemCompleted(item_id="old", text=""))])
    async with history.sessions() as session:
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
    assert item.text_ref["chunk_count"] == "0"

    # The fold stalls on what it does not model: nothing of the batch applies and the view reports it.
    await history.record(thread, [event_entry(5)])
    assert await event_logs.last_cursor(thread) == 4
    _, view = await view_state(history, thread)
    assert operational_state(view)["status"] == "failed"


async def test_a_completion_that_is_the_streamed_text_keeps_the_body_and_stores_nothing(
    event_logs: SeededEventLogStore, history: History
) -> None:
    thread = await event_logs.seed("sb-1", "s-1")
    await history.record(
        thread,
        [
            event_entry(1, text_delta=event_pb2.TextDelta(item_id="answer", text="hello")),
            event_entry(2, tool_output_delta=event_pb2.ToolOutputDelta(item_id="tool", text="out")),
        ],
    )
    # The answer's completion compares with a stored chunk and an append in its own batch; the
    # tool's with stored chunks alone.
    await history.record(
        thread,
        [
            event_entry(3, text_delta=event_pb2.TextDelta(item_id="answer", text=" world")),
            event_entry(4, item_completed=event_pb2.ItemCompleted(item_id="answer", text="hello world")),
            event_entry(
                5,
                item_completed=event_pb2.ItemCompleted(
                    item_id="tool", tool=event_pb2.ToolResult(output="out", succeeded=True)
                ),
            ),
        ],
    )
    async with history.sessions() as session:
        items = {
            row.entity_id: row
            for row in await session.scalars(
                select(ThreadEntity).where(
                    ThreadEntity.thread_id == thread, ThreadEntity.entity_kind == EntityKind.ITEM
                )
            )
        }
        chunks = (
            await session.scalars(
                select(ThreadPayloadChunk)
                .where(ThreadPayloadChunk.thread_id == thread)
                .order_by(ThreadPayloadChunk.owner_id, ThreadPayloadChunk.chunk_index)
            )
        ).all()
    answer, tool = items["answer"].text_ref, items["tool"].output_ref
    assert answer is not None
    assert tool is not None
    assert (answer["generation"], answer["revision_cursor"], answer["chunk_count"]) == ("1", "4", "2")
    assert (tool["generation"], tool["revision_cursor"], tool["chunk_count"]) == ("2", "5", "1")
    assert [(chunk.owner_id, chunk.generation, chunk.text) for chunk in chunks] == [
        ("answer", 1, "hello"),
        ("answer", 1, " world"),
        ("tool", 2, "out"),
    ]


async def test_record_projects_confirmed_input_and_parallel_tool_revisions(
    event_logs: SeededEventLogStore, history: History
) -> None:
    thread = await event_logs.seed("sb-1", "s-1")
    command = command_pb2.Command(command_id="input", submit_input=command_pb2.SubmitInput(text="question"))
    confirmed = event_entry(
        2,
        harness_user_message_confirmed=event_pb2.HarnessUserMessageConfirmed(
            harness_message_id="message", text="question", origin_command_ids=["input"], turn_id="turn"
        ),
    )
    confirmed.event.source_sequences.extend([81, 82])
    await history.record(
        thread,
        [
            event_entry(1, command_admitted=event_pb2.CommandAdmitted(command=command)),
            confirmed,
            event_entry(3, tool_arguments_delta=event_pb2.ToolArgumentsDelta(item_id="tool-a", partial_json="{")),
        ],
    )
    await history.record(
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
    )
    async with history.sessions() as session:
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
    event_logs: SeededEventLogStore, history: History
) -> None:
    thread = await event_logs.seed("sb-1", "s-1")
    await history.record(
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
    )
    await history.record(
        thread,
        [
            event_entry(3, text_delta=event_pb2.TextDelta(item_id="answer", text="late")),
            event_entry(4, tool_output_delta=event_pb2.ToolOutputDelta(item_id="tool", text="late")),
        ],
    )
    async with history.sessions() as session:
        rows = await session.scalars(
            select(ThreadEntity).where(ThreadEntity.thread_id == thread, ThreadEntity.entity_kind == EntityKind.ITEM)
        )
        completions = {
            row.entity_id: (row.revision_cursor, row.state["completion"], row.state["tool_succeeded"]) for row in rows
        }
    assert completions == {"answer": (3, "text", None), "tool": (4, "tool", False)}


async def test_every_row_is_numbered_densely_in_thread_order_and_never_renumbered(
    event_logs: SeededEventLogStore, history: History
) -> None:
    """The index is a position in the thread, so it is dense, ordered and fixed once given."""
    thread = await event_logs.seed("sb-1", "s-1")
    await history.record(
        thread,
        [
            event_entry(1, harness_started=event_pb2.HarnessStarted(pid=1)),
            event_entry(2, text_delta=event_pb2.TextDelta(item_id="first", text="a")),
            event_entry(3, text_delta=event_pb2.TextDelta(item_id="second", text="b")),
        ],
    )

    async def numbered() -> list[tuple[str, str, int]]:
        async with history.sessions() as session:
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
    await history.record(
        thread,
        [
            event_entry(4, text_delta=event_pb2.TextDelta(item_id="first", text="c")),
            event_entry(5, text_delta=event_pb2.TextDelta(item_id="third", text="d")),
        ],
    )
    after = await numbered()
    positions = {(kind, entity_id): index for kind, entity_id, index in after}
    before = {(kind, entity_id): index for kind, entity_id, index in first_pass}
    assert before.items() <= positions.items()
    assert [index for _, _, index in after] == list(range(len(after)))
    assert positions[("item", "third")] == len(first_pass)


if __name__ == "__main__":
    pytest_bazel.main()
