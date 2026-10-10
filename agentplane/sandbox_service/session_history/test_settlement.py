import json

import pytest
import pytest_bazel

from agentplane.native.delta_templates import DeltaTemplate
from agentplane.protocol import event_log_pb2, event_pb2
from agentplane.sandbox_service import protocol_pb2
from agentplane.sandbox_service.session_history.settlement import cursor_ranges, settle
from agentplane.sandbox_service.testing.delta_spans import (
    Span,
    claude_assistant,
    claude_delta,
    claude_text,
    claude_tool_arguments,
    codex_command,
    codex_reasoning,
)

# gazelle:include_dep @pypi//protobuf


def ranges(settlement: protocol_pb2.SettledDeltas) -> list[tuple[int, int]]:
    return [(cursor_range.first, cursor_range.last) for cursor_range in settlement.ranges]


def test_claude_text_settles_its_chunk_pairs() -> None:
    # 1-2 start, 3-6 two chunk pairs, 7-8 completion.
    settlement = settle(claude_text(["Hel", "lo"]).entries)
    assert settlement is not None
    assert (settlement.item_id, settlement.template, settlement.completion_cursor, settlement.chunk_count) == (
        "msg_test#0",
        DeltaTemplate.CLAUDE_TEXT,
        8,
        2,
    )
    assert ranges(settlement) == [(3, 6)]
    assert (settlement.first_chunk_at.ToMilliseconds(), settlement.last_chunk_at.ToMilliseconds()) == (4_000, 6_000)


def test_claude_arguments_compare_as_parsed_json() -> None:
    settlement = settle(claude_tool_arguments(['{"command":', ' "ls -la"}'], {"command": "ls -la"}).entries)
    assert settlement is not None
    assert (settlement.template, ranges(settlement)) == (DeltaTemplate.CLAUDE_INPUT_JSON, [(3, 6)])


def test_codex_reasoning_compares_each_summary_part() -> None:
    settlement = settle(codex_reasoning([["a", "b"], ["c"]]).entries)
    assert settlement is not None
    assert (settlement.template, settlement.chunk_count, ranges(settlement)) == (
        DeltaTemplate.CODEX_REASONING_SUMMARY,
        3,
        [(3, 8)],
    )


def test_codex_command_output_settles_when_aggregated_matches() -> None:
    assert settle(codex_command(["one\n", "two\n"], aggregated="one\ntwo\n").entries) is not None


@pytest.mark.parametrize("aggregated", [None, "one\n"])
def test_codex_command_output_without_matching_aggregate_keeps_chunks(aggregated: str | None) -> None:
    assert settle(codex_command(["one\n", "two\n"], aggregated=aggregated).entries) is None


def test_completion_that_differs_from_the_chunks_keeps_them() -> None:
    assert settle(claude_text(["Hel", "lo"], completed="Hello!").entries) is None


def test_arguments_that_differ_from_the_tool_input_keep_chunks() -> None:
    assert settle(claude_tool_arguments(['{"command": "ls"}'], {"command": "pwd"}).entries) is None


def test_anomaly_in_the_span_keeps_every_chunk() -> None:
    assert settle(claude_text(["Hel"], interpose=lambda span: span.stderr("harness complaint")).entries) is None


def test_unrecognised_chunk_frame_keeps_the_item() -> None:
    span = claude_text(["Hel", "lo"])
    frame = claude_delta(0, {"type": "text_delta", "text": "lo"}, "test-chunk-1")
    span.entries[4].event.native.line = json.dumps({**frame, "newer_harness_field": 1})
    assert settle(span.entries) is None


def test_duplicate_key_frame_keeps_the_item() -> None:
    span = claude_text(["Hel"])
    span.entries[2].event.native.line = span.entries[2].event.native.line[:-1] + ', "uuid": "again"}'
    assert settle(span.entries) is None


def test_chunk_frame_cited_by_another_event_keeps_the_item() -> None:
    def cite_chunk(span: Span) -> None:
        span.derived([3], event_pb2.Event(turn_started=event_pb2.TurnStarted(turn_id="test-turn")))

    assert settle(claude_text(["Hel"], interpose=cite_chunk).entries) is None


def test_chunk_of_another_message_keeps_the_item() -> None:
    span = claude_text(["Hel"])
    span.entries[-2].event.native.line = json.dumps(claude_assistant("msg_other", {"type": "text", "text": "Hel"}))
    assert settle(span.entries) is None


def test_non_completion_entry_never_settles() -> None:
    entries = claude_text(["Hel"]).entries
    assert settle(entries[:-1]) is None
    assert settle([event_log_pb2.EventEntry()]) is None


def test_cursor_ranges_merge_adjacent_cursors() -> None:
    assert cursor_ranges({3, 4, 5, 9, 11, 12}) == [(3, 5), (9, 9), (11, 12)]


if __name__ == "__main__":
    pytest_bazel.main()
