"""Assertions over the events one attachment saw."""

from __future__ import annotations

from collections.abc import Callable, Sequence

from x.agentplane.runner import protocol_pb2 as pb

# The generated protocol stubs' own stub chain, which the mypy aspect resolves for direct deps only.
# gazelle:include_dep @pypi//protobuf


def kind(event: pb.Event) -> str:
    return event.WhichOneof("observation") or ""


def of_kind(events: Sequence[pb.Event], name: str) -> list[pb.Event]:
    return [event for event in events if kind(event) == name]


def is_kind(name: str) -> Callable[[pb.Event], bool]:
    return lambda event: kind(event) == name


def turn_completed(event: pb.Event) -> bool:
    return kind(event) == "turn_completed"


def items(events: Sequence[pb.Event], item_kind: int) -> list[str]:
    """Ids of the items started with `item_kind`, in order."""
    return [
        event.item_started.item_id for event in of_kind(events, "item_started") if event.item_started.kind == item_kind
    ]


def streamed_text(events: Sequence[pb.Event], item_id: str) -> str:
    return "".join(
        event.text_delta.text for event in of_kind(events, "text_delta") if event.text_delta.item_id == item_id
    )


def completed(events: Sequence[pb.Event], item_id: str) -> pb.ItemCompleted:
    (event,) = [event for event in of_kind(events, "item_completed") if event.item_completed.item_id == item_id]
    return event.item_completed


def tool_arguments(events: Sequence[pb.Event], item_id: str) -> str:
    (event,) = [event for event in of_kind(events, "tool_arguments") if event.tool_arguments.item_id == item_id]
    return event.tool_arguments.arguments_json


def assert_contiguous(events: Sequence[pb.Event]) -> None:
    """Sequences are dense and increasing: the log has neither gaps nor duplicates."""
    sequences = [event.sequence for event in events]
    assert sequences == list(range(sequences[0], sequences[0] + len(sequences))), sequences


def assert_sourced(events: Sequence[pb.Event]) -> None:
    """Every event derived from harness output names the Native events it came from."""
    native = {
        event.sequence for event in of_kind(events, "native") if event.native.direction == pb.DIRECTION_FROM_HARNESS
    }
    derived = (
        "item_started",
        "text_delta",
        "tool_arguments_delta",
        "tool_arguments",
        "tool_output_delta",
        "item_completed",
        "turn_completed",
    )
    for event in events:
        if kind(event) in derived:
            assert event.source_sequences, event
            assert set(event.source_sequences) <= native, event
