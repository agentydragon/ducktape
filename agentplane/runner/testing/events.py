"""Assertions over the events one attachment saw."""

from __future__ import annotations

from collections.abc import Callable, Sequence

from agentplane.protocol import event_log_pb2, event_pb2

# The generated protocol stubs' own stub chain, which the mypy aspect resolves for direct deps only.
# gazelle:include_dep @pypi//protobuf


def kind(entry: event_log_pb2.EventEntry) -> str:
    return entry.event.WhichOneof("observation") or ""


def of_kind(entries: Sequence[event_log_pb2.EventEntry], name: str) -> list[event_log_pb2.EventEntry]:
    return [entry for entry in entries if kind(entry) == name]


def is_kind(name: str) -> Callable[[event_log_pb2.EventEntry], bool]:
    return lambda entry: kind(entry) == name


def turn_completed(entry: event_log_pb2.EventEntry) -> bool:
    return kind(entry) == "turn_completed"


def items(entries: Sequence[event_log_pb2.EventEntry], item_kind: int) -> list[str]:
    """Ids of the items started with `item_kind`, in order."""
    return [
        entry.event.item_started.item_id
        for entry in of_kind(entries, "item_started")
        if entry.event.item_started.kind == item_kind
    ]


def streamed_text(entries: Sequence[event_log_pb2.EventEntry], item_id: str) -> str:
    return "".join(
        entry.event.text_delta.text
        for entry in of_kind(entries, "text_delta")
        if entry.event.text_delta.item_id == item_id
    )


def completed(entries: Sequence[event_log_pb2.EventEntry], item_id: str) -> event_pb2.ItemCompleted:
    (entry,) = [entry for entry in of_kind(entries, "item_completed") if entry.event.item_completed.item_id == item_id]
    return entry.event.item_completed


def tool_arguments(entries: Sequence[event_log_pb2.EventEntry], item_id: str) -> str:
    (entry,) = [entry for entry in of_kind(entries, "tool_arguments") if entry.event.tool_arguments.item_id == item_id]
    return entry.event.tool_arguments.arguments_json


def assert_contiguous(entries: Sequence[event_log_pb2.EventEntry]) -> None:
    """Source-local cursors are dense and increasing: the log has neither gaps nor duplicates."""
    cursors = [entry.cursor for entry in entries]
    assert cursors == list(range(cursors[0], cursors[0] + len(cursors))), cursors


def assert_sourced(entries: Sequence[event_log_pb2.EventEntry]) -> None:
    """Every event derived from harness output names the Native events it came from."""
    native = {
        entry.origin.sequence
        for entry in of_kind(entries, "native")
        if entry.event.native.direction == event_pb2.DIRECTION_FROM_HARNESS
    }
    derived = (
        "item_started",
        "text_delta",
        "tool_arguments_delta",
        "tool_arguments",
        "tool_output_delta",
        "item_completed",
        "turn_completed",
        "harness_user_message_confirmed",
        "model_changed",
    )
    for entry in entries:
        if kind(entry) in derived:
            assert entry.event.source_sequences, entry
            assert set(entry.event.source_sequences) <= native, entry
