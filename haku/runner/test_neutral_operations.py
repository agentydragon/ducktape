"""Contract fixtures for the neutral-operation journal.

The files under `testdata/` are the cross-stage contract: the runner-side projector and the
Console journal consumer both load the same lines, so the two halves cannot drift apart while they
are built against this module. The builders below are the source of truth and the checked-in lines
are their pinned output — the round-trip tests run the (de)serializer both ways, so the fixture is
generated output, not a change detector.

Wire shape only. A segment naming an item no `item.opened` introduced, a batch seq the journal
already committed, an admission frontier ahead of the cursor — those are the consumer's lifecycle
invariants, deliberately not validated here.
"""

import json
from pathlib import Path
from uuid import UUID

import pytest
import pytest_bazel
from pydantic import ValidationError

from haku.runner.neutral_operations import (
    CONSOLE_TO_RUNNER,
    NEUTRAL_PROTOCOL_VERSION,
    RUNNER_TO_CONSOLE,
    BatchAck,
    BatchDiagnostics,
    ConsoleResume,
    ConsoleToRunner,
    FrameRange,
    ItemCompleted,
    ItemOpened,
    ItemSegment,
    ItemType,
    Json,
    MessageCompletion,
    MessageOpen,
    OpaqueCause,
    OperationBatch,
    PromptAdmitted,
    PromptsCause,
    ReasoningCompletion,
    ReasoningDisclosure,
    ReasoningOpen,
    RunnerHello,
    RunnerToConsole,
    ToolCallCompletion,
    ToolCallOpen,
    ToolOutcome,
    TurnAborted,
    TurnAnswered,
    TurnEnded,
    TurnFailed,
    TurnOpened,
    TurnOutcome,
    WakeCause,
)
from util.bazel.runfiles import get_required_path

_TESTDATA = "haku/runner/testdata"
_RUNNER_GOLDEN = "neutral_v1_runner_to_console.jsonl"
_CONSOLE_GOLDEN = "neutral_v1_console_to_runner.jsonl"

_PROMPT_1 = UUID("11111111-1111-4111-8111-111111111101")
_PROMPT_2 = UUID("11111111-1111-4111-8111-111111111102")
_TURN_1 = UUID("22222222-2222-4222-8222-222222222201")
_TURN_2 = UUID("22222222-2222-4222-8222-222222222202")
_TURN_3 = UUID("22222222-2222-4222-8222-222222222203")
_TURN_4 = UUID("22222222-2222-4222-8222-222222222204")
_MESSAGE = UUID("33333333-3333-4333-8333-333333333301")
_TOOL_CALL = UUID("33333333-3333-4333-8333-333333333302")
_REASONING = UUID("33333333-3333-4333-8333-333333333303")


def _range(first: int, last: int) -> FrameRange:
    return FrameRange(first_frame_seq=first, last_frame_seq=last)


# The session the golden files record: two answered/failed prompt turns, a wake turn, and a turn
# still open when the journal stops — a session is terminal on runner loss, so a journal may end
# mid-turn.
_RUNNER_MESSAGES: tuple[RunnerToConsole, ...] = (
    RunnerHello(),
    OperationBatch(
        neutral_protocol_version=NEUTRAL_PROTOCOL_VERSION,
        runner_batch_seq=1,
        operations=(
            PromptAdmitted(prompt_id=_PROMPT_1, after_batch_seq=None, provenance=None),
            TurnOpened(turn_id=_TURN_1, cause=PromptsCause(prompt_ids=(_PROMPT_1,)), provenance=None),
            ItemOpened(item_id=_MESSAGE, turn_id=_TURN_1, item=MessageOpen(), provenance=_range(12, 12)),
            ItemSegment(item_id=_MESSAGE, text="Checking the build", provenance=_range(13, 13)),
            ItemSegment(item_id=_MESSAGE, text=" now.", provenance=_range(14, 14)),
        ),
    ),
    OperationBatch(
        neutral_protocol_version=NEUTRAL_PROTOCOL_VERSION,
        runner_batch_seq=2,
        operations=(
            ItemCompleted(
                item_id=_MESSAGE,
                completion=MessageCompletion(),
                backend_item_id="msg_0195mgqkkd",
                provenance=_range(12, 15),
            ),
            TurnEnded(turn_id=_TURN_1, end=TurnAnswered(), provenance=_range(15, 15)),
        ),
        diagnostics=BatchDiagnostics(unprojected={"tool_progress": 3}),
    ),
    OperationBatch(
        neutral_protocol_version=NEUTRAL_PROTOCOL_VERSION,
        runner_batch_seq=3,
        operations=(
            PromptAdmitted(prompt_id=_PROMPT_2, after_batch_seq=2, provenance=_range(16, 16)),
            TurnOpened(turn_id=_TURN_2, cause=PromptsCause(prompt_ids=(_PROMPT_2,)), provenance=_range(17, 17)),
            ItemOpened(
                item_id=_TOOL_CALL,
                turn_id=_TURN_2,
                item=ToolCallOpen(
                    tool_name="Bash",
                    arguments={"command": "bazel test //haku/...", "description": "Run the affected tests"},
                ),
                backend_item_id="toolu_01AbCdEfGh",
                provenance=_range(18, 20),
            ),
            ItemSegment(item_id=_TOOL_CALL, text="3 tests passed.", provenance=_range(21, 21)),
            ItemCompleted(
                item_id=_TOOL_CALL,
                completion=ToolCallCompletion(outcome=ToolOutcome.SUCCEEDED, structured={"exitCode": 0}),
                provenance=_range(21, 21),
            ),
            ItemOpened(item_id=_REASONING, turn_id=_TURN_2, item=ReasoningOpen(), provenance=_range(22, 22)),
            ItemSegment(item_id=_REASONING, text="The exit code settles it.", provenance=_range(22, 22)),
            ItemCompleted(
                item_id=_REASONING,
                completion=ReasoningCompletion(disclosure=ReasoningDisclosure.SUMMARY),
                provenance=_range(22, 23),
            ),
            TurnEnded(
                turn_id=_TURN_2,
                end=TurnFailed(failure="provider disconnected: overloaded_error"),
                provenance=_range(24, 24),
            ),
        ),
    ),
    OperationBatch(
        neutral_protocol_version=NEUTRAL_PROTOCOL_VERSION,
        runner_batch_seq=4,
        operations=(
            TurnOpened(turn_id=_TURN_3, cause=WakeCause(), provenance=_range(25, 25)),
            TurnEnded(turn_id=_TURN_3, end=TurnAborted(), provenance=None),
            TurnOpened(turn_id=_TURN_4, cause=OpaqueCause(), provenance=_range(26, 26)),
        ),
    ),
)

# The final ACK answers batches 3 and 4 at once: ACKs are cumulative, which is what lets the
# runner coalesce while one is in flight and drop its whole retention window on the reply.
_CONSOLE_MESSAGES: tuple[ConsoleToRunner, ...] = (
    ConsoleResume(neutral_protocol_version=NEUTRAL_PROTOCOL_VERSION, acked_batch_seq=None),
    BatchAck(acked_batch_seq=1),
    BatchAck(acked_batch_seq=2),
    BatchAck(acked_batch_seq=4),
)


def _golden_lines(name: str) -> list[str]:
    source = Path(f"{_TESTDATA}/{name}")
    path = source if source.exists() else get_required_path(f"ducktape/{_TESTDATA}/{name}")
    return path.read_text().splitlines()


def test_runner_to_console_golden_round_trips():
    lines = _golden_lines(_RUNNER_GOLDEN)
    assert len(lines) == len(_RUNNER_MESSAGES)
    for line, message in zip(lines, _RUNNER_MESSAGES, strict=True):
        assert RUNNER_TO_CONSOLE.validate_json(line) == message
        assert json.loads(line) == json.loads(message.model_dump_json())


def test_console_to_runner_golden_round_trips():
    lines = _golden_lines(_CONSOLE_GOLDEN)
    for line, message in zip(lines, _CONSOLE_MESSAGES, strict=True):
        assert CONSOLE_TO_RUNNER.validate_json(line) == message
        assert json.loads(line) == json.loads(message.model_dump_json())


def test_golden_covers_the_whole_vocabulary():
    """The fixture stays representative: every operation kind and every union arm appears, so a
    stage building against the golden files has exercised the full settled vocabulary."""
    operations = [
        operation
        for message in _RUNNER_MESSAGES
        if isinstance(message, OperationBatch)
        for operation in message.operations
    ]
    assert {operation.kind for operation in operations} == {
        "turn.opened",
        "turn.ended",
        "prompt.admitted",
        "item.opened",
        "item.segment",
        "item.completed",
    }
    assert {op.item.item_type for op in operations if isinstance(op, ItemOpened)} == set(ItemType)
    assert {op.completion.item_type for op in operations if isinstance(op, ItemCompleted)} == set(ItemType)
    assert {op.end.outcome for op in operations if isinstance(op, TurnEnded)} == set(TurnOutcome)
    assert {op.cause.kind for op in operations if isinstance(op, TurnOpened)} == {"prompts", "wake", "opaque"}


def test_a_replayed_batch_is_the_same_batch():
    """Idempotency is by value: a batch replayed after a reconnect parses equal to the original,
    so a consumer keyed on `runner_batch_seq` commits it once and the replay changes nothing."""
    line = _golden_lines(_RUNNER_GOLDEN)[3]
    original = RUNNER_TO_CONSOLE.validate_json(line)
    replayed = RUNNER_TO_CONSOLE.validate_json(line)
    assert replayed == original
    assert isinstance(replayed, OperationBatch)
    assert replayed.runner_batch_seq == 3


def _wire_batch(*operations: dict[str, Json], unprojected: dict[str, int] | None = None) -> str:
    return json.dumps(
        {
            "kind": "batch",
            "neutral_protocol_version": 1,
            "runner_batch_seq": 1,
            "operations": list(operations),
            "diagnostics": {"unprojected": unprojected or {}},
        }
    )


def _segment(**overrides: Json) -> dict[str, Json]:
    base: dict[str, Json] = {
        "kind": "item.segment",
        "item_id": str(_MESSAGE),
        "text": "hi",
        "provenance": {"first_frame_seq": 1, "last_frame_seq": 1},
    }
    return base | overrides


def test_tool_call_opens_only_with_complete_arguments():
    # Arguments missing: "a call is being composed" is not expressible on this wire.
    with pytest.raises(ValidationError):
        RUNNER_TO_CONSOLE.validate_json(
            _wire_batch(
                {
                    "kind": "item.opened",
                    "item_id": str(_TOOL_CALL),
                    "turn_id": None,
                    "item": {"item_type": "tool_call", "tool_name": "Bash"},
                    "backend_item_id": None,
                    "provenance": {"first_frame_seq": 1, "last_frame_seq": 1},
                }
            )
        )
    # Arguments as a partial-JSON string rather than the finished object.
    with pytest.raises(ValidationError):
        RUNNER_TO_CONSOLE.validate_json(
            _wire_batch(
                {
                    "kind": "item.opened",
                    "item_id": str(_TOOL_CALL),
                    "turn_id": None,
                    "item": {"item_type": "tool_call", "tool_name": "Bash", "arguments": '{"command": "bazel'},
                    "backend_item_id": None,
                    "provenance": {"first_frame_seq": 1, "last_frame_seq": 1},
                }
            )
        )


def test_unknown_operation_kind_rejects():
    """A kind outside the settled version fails the union parse: on this negotiated seam a
    must-understand change arrives as a new version, never as a kind to skip."""
    with pytest.raises(ValidationError):
        RUNNER_TO_CONSOLE.validate_json(_wire_batch({"kind": "item.redacted", "item_id": str(_MESSAGE)}))


def test_unknown_message_kind_rejects():
    with pytest.raises(ValidationError):
        RUNNER_TO_CONSOLE.validate_json('{"kind":"telemetry","payload":{}}')
    with pytest.raises(ValidationError):
        CONSOLE_TO_RUNNER.validate_json('{"kind":"telemetry","payload":{}}')


def test_additive_fields_are_ignored():
    """The other half of the roll rule: a field added by a newer peer must not kill the session."""
    line = _golden_lines(_RUNNER_GOLDEN)[1]
    padded = json.loads(line)
    padded["debug_note"] = "added by a newer runner"
    padded["operations"][0]["urgency"] = "high"
    assert RUNNER_TO_CONSOLE.validate_json(json.dumps(padded)) == _RUNNER_MESSAGES[1]


def test_version_this_image_cannot_speak_rejects():
    batch = json.loads(_wire_batch(_segment()))
    batch["neutral_protocol_version"] = 99
    with pytest.raises(ValidationError):
        RUNNER_TO_CONSOLE.validate_json(json.dumps(batch))
    with pytest.raises(ValidationError):
        CONSOLE_TO_RUNNER.validate_json(
            '{"kind":"resume","generation":"runner_projection_v1","neutral_protocol_version":99,"acked_batch_seq":null}'
        )


def test_segments_carry_prose():
    # An empty segment says nothing and would let "the segments are the text" hide empty writes.
    with pytest.raises(ValidationError):
        RUNNER_TO_CONSOLE.validate_json(_wire_batch(_segment(text="")))


def test_a_failed_turn_states_its_failure():
    with pytest.raises(ValidationError):
        RUNNER_TO_CONSOLE.validate_json(
            _wire_batch(
                {"kind": "turn.ended", "turn_id": str(_TURN_1), "end": {"outcome": "failed"}, "provenance": None}
            )
        )


def test_a_frame_range_runs_forward():
    with pytest.raises(ValidationError):
        RUNNER_TO_CONSOLE.validate_json(_wire_batch(_segment(provenance={"first_frame_seq": 5, "last_frame_seq": 4})))


def test_a_turn_names_its_prompts_or_a_different_cause():
    # `prompts` with an empty list would claim prompt causation while naming nobody.
    with pytest.raises(ValidationError):
        RUNNER_TO_CONSOLE.validate_json(
            _wire_batch(
                {
                    "kind": "turn.opened",
                    "turn_id": str(_TURN_1),
                    "cause": {"kind": "prompts", "prompt_ids": []},
                    "provenance": None,
                }
            )
        )


def test_a_batch_says_something():
    with pytest.raises(ValidationError):
        RUNNER_TO_CONSOLE.validate_json(_wire_batch())
    # Operations may be absent when there is still something to say: a stretch of frames that
    # projects to nothing but should not vanish from observability.
    diagnostics_only = RUNNER_TO_CONSOLE.validate_json(_wire_batch(unprojected={"codex/token_count": 2}))
    assert isinstance(diagnostics_only, OperationBatch)
    assert diagnostics_only.diagnostics.unprojected == {"codex/token_count": 2}
    # A zero count is dead payload, not a report.
    with pytest.raises(ValidationError):
        RUNNER_TO_CONSOLE.validate_json(_wire_batch(unprojected={"codex/token_count": 0}))


def test_journal_numbers_start_at_one():
    batch = json.loads(_wire_batch(_segment()))
    batch["runner_batch_seq"] = 0
    with pytest.raises(ValidationError):
        RUNNER_TO_CONSOLE.validate_json(json.dumps(batch))
    with pytest.raises(ValidationError):
        CONSOLE_TO_RUNNER.validate_json('{"kind":"ack","acked_batch_seq":0}')


if __name__ == "__main__":
    pytest_bazel.main()
