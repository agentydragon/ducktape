"""What the runner-side Claude projector does with the shapes production actually sends.

Each test is named for the hazard it pins, and the hazards are the ones the adopted #4667 design
names for Claude: unkeyed text deltas, partial `input_json_delta` composition, tool results
interspersed inside an open assistant message, deduplication when a result precedes the completed
assistant block, and prompts folded into an active turn at a tool boundary. The frame builders are
lean local ports of the shapes the Console-side fixtures pinned
(`haku/console/x/claude_code/testing/wire.py`, deletion-scheduled with its package — the wire
dependency direction forbids importing it from the runtime).

`test_the_recorded_capture_folds_to_the_session_it_recorded` folds a session verbatim
(`testdata/diverse_session.jsonl`): every frame Claude Code 2.1.233 emitted while writing a
file, reading it back, counting its lines, running a command that fails, and summarising —
including live text/thinking/argument deltas, so the stream path meets frames nobody composed.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Callable, Iterable, Sequence
from itertools import count
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
import pytest_bazel

from haku.runner.claude.projection import ClaudeProjector, undelivered
from haku.runner.neutral_operations import (
    NEUTRAL_PROTOCOL_VERSION,
    RUNNER_TO_CONSOLE,
    FrameRange,
    ItemCompleted,
    ItemOpened,
    ItemSegment,
    ItemType,
    MessageCompletion,
    MessageOpen,
    Operation,
    OperationBatch,
    PromptAdmitted,
    PromptsCause,
    ReasoningCompletion,
    ReasoningDisclosure,
    ReasoningOpen,
    ToolCallCompletion,
    ToolCallOpen,
    ToolOutcome,
    TurnAnswered,
    TurnEnded,
    TurnFailed,
    TurnOpened,
    WakeCause,
)
from haku.runner.operation_journal import OperationJournal
from util.bazel.runfiles import get_required_path

BASH_RESULT = {"interrupted": False, "isImage": False, "stderr": "", "stdout": "3\n"}

_PROMPT_1 = UUID("11111111-1111-4111-8111-111111111101")
_PROMPT_2 = UUID("11111111-1111-4111-8111-111111111102")


def minted() -> Callable[[], UUID]:
    """Deterministic runner ids: the N-th minted id is `UUID(int=N)`."""
    ids = count(1)
    return lambda: UUID(int=next(ids))


def projector() -> ClaudeProjector:
    return ClaudeProjector(mint_id=minted())


def fold(subject: ClaudeProjector, payloads: Sequence[dict[str, Any]]) -> tuple[tuple[Operation, ...], dict[str, int]]:
    """Observe *payloads* as frames 1..n, as one accumulated yield."""
    operations: list[Operation] = []
    misses: Counter[str] = Counter()
    for frame_seq, payload in enumerate(payloads, start=1):
        projected = subject.observe(frame_seq, payload)
        operations.extend(projected.operations)
        misses.update(projected.unprojected)
    return tuple(operations), dict(misses)


def assert_wire_round_trips(operations: Iterable[Operation]) -> None:
    """Every emitted operation serializes and parses back through the settled wire adapters."""
    batch = OperationBatch(
        neutral_protocol_version=NEUTRAL_PROTOCOL_VERSION, runner_batch_seq=1, operations=tuple(operations)
    )
    assert RUNNER_TO_CONSOLE.validate_json(batch.model_dump_json()) == batch


def _range(first: int, last: int) -> FrameRange:
    return FrameRange(first_frame_seq=first, last_frame_seq=last)


# --- frame builders -------------------------------------------------------------------------


def text_block(text: str) -> dict[str, Any]:
    return {"text": text, "type": "text"}


def thinking_block(thinking: str) -> dict[str, Any]:
    return {"signature": "EqQBCkYIBxgCK", "thinking": thinking, "type": "thinking"}


def tool_use_block(call_id: str, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return {"id": call_id, "input": arguments, "name": name, "type": "tool_use"}


def assistant(*blocks: dict[str, Any], message_id: str | None = None) -> dict[str, Any]:
    message: dict[str, Any] = {"content": list(blocks), "role": "assistant", "stop_reason": None, "type": "message"}
    if message_id is not None:
        message["id"] = message_id
    return {"message": message, "session_id": "a2d5", "type": "assistant"}


def tool_result(call_id: str, content: Any, *, structured: Any = None, is_error: bool | None = None) -> dict[str, Any]:
    """An inbound `user` frame: what a tool answered. `is_error=None` omits the key entirely,
    which is the wire's routine shape rather than a shorthand for false."""
    block: dict[str, Any] = {"content": content, "tool_use_id": call_id, "type": "tool_result"}
    if is_error is not None:
        block["is_error"] = is_error
    return {"message": {"content": [block], "role": "user"}, "tool_use_result": structured, "type": "user"}


def injected_command(text: str) -> dict[str, Any]:
    """A `user` frame with string content: the harness speaking as the user."""
    return {"message": {"content": text, "role": "user"}, "type": "user"}


def stream_event(event: dict[str, Any]) -> dict[str, Any]:
    return {"event": event, "type": "stream_event"}


def text_delta(text: str) -> dict[str, Any]:
    return stream_event({"delta": {"text": text, "type": "text_delta"}, "index": 0, "type": "content_block_delta"})


def tool_use_start(call_id: str, name: str, *, index: int = 0) -> dict[str, Any]:
    return stream_event(
        {"content_block": tool_use_block(call_id, name, {}), "index": index, "type": "content_block_start"}
    )


def input_json_delta(partial_json: str, *, index: int = 0) -> dict[str, Any]:
    return stream_event(
        {
            "delta": {"partial_json": partial_json, "type": "input_json_delta"},
            "index": index,
            "type": "content_block_delta",
        }
    )


def content_block_stop(*, index: int = 0) -> dict[str, Any]:
    return stream_event({"index": index, "type": "content_block_stop"})


def result(*, subtype: str = "success", stop_reason: str | None = "end_turn") -> dict[str, Any]:
    return {"is_error": False, "result": "", "stop_reason": stop_reason, "subtype": subtype, "type": "result"}


def system(subtype: str, **fields: Any) -> dict[str, Any]:
    return {"session_id": "a2d5", "subtype": subtype, "type": "system"} | fields


def heartbeat() -> dict[str, Any]:
    return system("thinking_tokens", estimated_tokens=1_024)


# --- the Claude quirks, case by case --------------------------------------------------------


def test_unkeyed_text_deltas_attach_to_the_open_message():
    """A delta carries no `message.id` to key by: the first opens a runner-minted message at its
    own frame, the rest join it, and the completed block does not repeat prose the deltas already
    delivered — it only supplies the backend id the wire had not stated yet."""
    operations, misses = fold(
        projector(),
        [
            text_delta("Looking at "),
            text_delta("the migration."),
            assistant(text_block("Looking at the migration."), message_id="msg_A"),
            result(),
        ],
    )

    turn, message = UUID(int=1), UUID(int=2)
    assert operations == (
        TurnOpened(turn_id=turn, cause=WakeCause(), provenance=_range(1, 1)),
        ItemOpened(item_id=message, turn_id=turn, item=MessageOpen(), provenance=_range(1, 1)),
        ItemSegment(item_id=message, text="Looking at ", provenance=_range(1, 1)),
        ItemSegment(item_id=message, text="the migration.", provenance=_range(2, 2)),
        ItemCompleted(
            item_id=message, completion=MessageCompletion(), backend_item_id="msg_A", provenance=_range(1, 3)
        ),
        TurnEnded(turn_id=turn, end=TurnAnswered(), provenance=_range(4, 4)),
    )
    assert misses == {}
    assert_wire_round_trips(operations)


def test_a_completed_block_streams_only_what_the_deltas_did_not():
    """A block completed past the watermark emits the remainder as one segment, so the item's
    concatenation is the block's text exactly once."""
    operations, _ = fold(
        projector(),
        [text_delta("Looking at "), assistant(text_block("Looking at the migration."), message_id="msg_A"), result()],
    )

    segments = [operation for operation in operations if isinstance(operation, ItemSegment)]
    assert [segment.text for segment in segments] == ["Looking at ", "the migration."]
    assert len({segment.item_id for segment in segments}) == 1


def test_a_message_with_no_prose_in_it_is_not_a_message():
    """Frames sharing one `message.id` that carry only thinking and a call open no message item:
    what happened was a thought and a call, which are their own items and say so."""
    operations, misses = fold(
        projector(),
        [
            assistant(thinking_block("Two revisions share an id."), message_id="msg_A"),
            assistant(tool_use_block("toolu_1", "Bash", {"command": "ls"}), message_id="msg_A"),
            result(),
        ],
    )

    turn, reasoning, call = UUID(int=1), UUID(int=2), UUID(int=3)
    assert operations == (
        TurnOpened(turn_id=turn, cause=WakeCause(), provenance=_range(1, 1)),
        ItemOpened(item_id=reasoning, turn_id=turn, item=ReasoningOpen(), provenance=_range(1, 1)),
        ItemSegment(item_id=reasoning, text="Two revisions share an id.", provenance=_range(1, 1)),
        ItemCompleted(
            item_id=reasoning,
            completion=ReasoningCompletion(disclosure=ReasoningDisclosure.SUMMARY),
            provenance=_range(1, 1),
        ),
        ItemOpened(
            item_id=call,
            turn_id=turn,
            item=ToolCallOpen(tool_name="Bash", arguments={"command": "ls"}),
            backend_item_id="toolu_1",
            provenance=_range(2, 2),
        ),
        TurnEnded(turn_id=turn, end=TurnAnswered(), provenance=_range(3, 3)),
    )
    assert misses == {}


def test_thinking_the_backend_will_not_show_you_is_an_item_with_no_segments():
    operations, _ = fold(projector(), [assistant({"type": "redacted_thinking", "data": "…"}, message_id="msg_A")])

    completions = [operation for operation in operations if isinstance(operation, ItemCompleted)]
    assert [completion.completion for completion in completions] == [
        ReasoningCompletion(disclosure=ReasoningDisclosure.WITHHELD)
    ]
    assert not any(isinstance(operation, ItemSegment) for operation in operations)


def test_partial_input_json_delta_composes_the_call():
    """Arguments stream as fragments; the call opens only at the frame that completes the object,
    with provenance spanning the whole composition — "a call is being composed" is never said."""
    operations, misses = fold(
        projector(),
        [
            tool_use_start("toolu_1", "Bash", index=1),
            input_json_delta('{"comm', index=1),
            input_json_delta('and": "true"}', index=1),
            content_block_stop(index=1),
        ],
    )

    opened = [operation for operation in operations if isinstance(operation, ItemOpened)]
    assert [operation.item for operation in opened] == [ToolCallOpen(tool_name="Bash", arguments={"command": "true"})]
    assert opened[0].provenance == _range(1, 4)
    assert opened[0].backend_item_id == "toolu_1"
    assert misses == {}


@pytest.mark.parametrize("fragments", [['{"broken'], ['["not", "an", "object"]']])
def test_a_malformed_composition_is_counted_rather_than_opened(fragments: list[str]):
    operations, misses = fold(
        projector(),
        [
            tool_use_start("toolu_1", "Bash", index=1),
            *[input_json_delta(fragment, index=1) for fragment in fragments],
            content_block_stop(index=1),
        ],
    )

    assert not any(
        isinstance(operation, ItemOpened) and isinstance(operation.item, ToolCallOpen) for operation in operations
    )
    assert misses == {"stream_event/tool_use_arguments": 1}


def test_tool_results_interspersed_inside_an_open_assistant_message():
    """The fixture's split-message shape: parallel calls asked inside one message, the first
    answered before the second is asked. A `user` frame never closes the message, and the
    message's completion reports the frames its prose came from."""
    operations, _ = fold(
        projector(),
        [
            assistant(text_block("reading both"), message_id="msg_A"),
            assistant(tool_use_block("toolu_1", "Read", {"file_path": "/a"}), message_id="msg_A"),
            tool_result("toolu_1", "1\tcontents\n", structured={"type": "text"}),
            assistant(tool_use_block("toolu_2", "Read", {"file_path": "/b"}), message_id="msg_A"),
            result(),
        ],
    )

    assert [type(operation) for operation in operations] == [
        TurnOpened,
        ItemOpened,  # the message
        ItemSegment,
        ItemOpened,  # toolu_1
        ItemSegment,  # toolu_1's rendered result
        ItemCompleted,  # toolu_1
        ItemOpened,  # toolu_2
        ItemCompleted,  # the message, closed by the result frame and not by the interruptions
        TurnEnded,
    ]
    message_completed = operations[-2]
    assert isinstance(message_completed, ItemCompleted)
    assert message_completed.backend_item_id == "msg_A"
    assert message_completed.provenance == _range(1, 1)
    assert_wire_round_trips(operations)


def test_a_result_preceding_the_completed_assistant_block_deduplicates():
    """Claude Code 2.1.220 can execute a streamed call and return its result before emitting the
    completed `assistant` block that used to declare it. The stream is the first full account, and
    the later block copy must not open a second item."""
    operations, misses = fold(
        projector(),
        [
            tool_use_start("toolu_1", "Bash", index=1),
            input_json_delta('{"command": "true"}', index=1),
            content_block_stop(index=1),
            tool_result("toolu_1", "ok", structured=BASH_RESULT, is_error=False),
            assistant(tool_use_block("toolu_1", "Bash", {"command": "true"}), message_id="msg_A"),
        ],
    )

    opened = [operation for operation in operations if isinstance(operation, ItemOpened)]
    assert len(opened) == 1
    outcomes = [
        operation.completion.outcome
        for operation in operations
        if isinstance(operation, ItemCompleted) and isinstance(operation.completion, ToolCallCompletion)
    ]
    assert outcomes == [ToolOutcome.SUCCEEDED]
    assert misses == {}


def test_a_result_outrunning_the_block_stop_finishes_the_composition_first():
    """The result can arrive while the argument stream is still open — no `content_block_stop`
    yet. The fragments already seen are the whole account there will be: the call opens from them,
    bounded by the composition's own frames, and is then answered; the stop and the block copy
    that follow say nothing new."""
    operations, misses = fold(
        projector(),
        [
            tool_use_start("toolu_1", "Bash", index=1),
            input_json_delta('{"command": "true"}', index=1),
            tool_result("toolu_1", "ok", structured=BASH_RESULT, is_error=False),
            content_block_stop(index=1),
            assistant(tool_use_block("toolu_1", "Bash", {"command": "true"}), message_id="msg_A"),
        ],
    )

    opened = [operation for operation in operations if isinstance(operation, ItemOpened)]
    assert len(opened) == 1
    assert opened[0].provenance == _range(1, 2)
    completed = [operation for operation in operations if isinstance(operation, ItemCompleted)]
    assert len(completed) == 1
    assert completed[0].item_id == opened[0].item_id
    assert misses == {}


def test_a_prompt_admitted_while_idle_opens_the_turn_it_causes():
    subject = projector()

    projected = subject.admit(_PROMPT_1, after_batch_seq=None)

    turn = UUID(int=1)
    assert projected.operations == (
        PromptAdmitted(prompt_id=_PROMPT_1, after_batch_seq=None, provenance=None),
        TurnOpened(turn_id=turn, cause=PromptsCause(prompt_ids=(_PROMPT_1,)), provenance=None),
    )
    assert projected.unprojected == {}


def test_a_prompt_admitted_mid_turn_is_a_fence_not_a_bracket():
    """The CLI folds a queued prompt into the active turn at a tool boundary, so the admission
    inside an open turn emits only `prompt.admitted` — a second bracket would claim an exchange
    that is not happening — and the one `result` still closes the one turn."""
    subject = projector()
    opening = subject.admit(_PROMPT_1, after_batch_seq=None)
    first_turn = UUID(int=1)

    subject.observe(1, assistant(tool_use_block("toolu_1", "Bash", {"command": "true"}), message_id="msg_A"))
    folded = subject.admit(_PROMPT_2, after_batch_seq=3, frame_seq=2)

    assert folded.operations == (PromptAdmitted(prompt_id=_PROMPT_2, after_batch_seq=3, provenance=_range(2, 2)),)

    ended = subject.observe(3, tool_result("toolu_1", "ok")).operations + subject.observe(4, result()).operations
    assert [operation for operation in ended if isinstance(operation, TurnEnded)] == [
        TurnEnded(turn_id=first_turn, end=TurnAnswered(), provenance=_range(4, 4))
    ]

    # Idle again, the next admission opens the next bracket.
    reopened = subject.admit(_PROMPT_1, after_batch_seq=5)
    turn_opens = [
        operation for operation in (*opening.operations, *reopened.operations) if isinstance(operation, TurnOpened)
    ]
    assert len(turn_opens) == 2
    assert turn_opens[0].turn_id != turn_opens[1].turn_id


def test_content_arriving_while_idle_is_a_wake_turn():
    """The harness resumed itself: the first content frame opens the bracket, at that frame."""
    operations, _ = fold(projector(), [heartbeat(), assistant(text_block("done"), message_id="msg_A"), result()])

    opened = [operation for operation in operations if isinstance(operation, TurnOpened)]
    assert [type(turn.cause) for turn in opened] == [WakeCause]
    assert opened[0].provenance == _range(2, 2)


def test_an_injected_command_the_cli_echoes_is_a_wake_but_not_prose():
    """An idle `user` frame with text is the harness speaking — it opens the bracket — and its
    text is deliberately not an item: the runner never authors prose, and the raw frame keeps it."""
    operations, misses = fold(projector(), [injected_command("Check on the background build.")])

    assert [type(operation) for operation in operations] == [TurnOpened]
    assert misses == {}


def test_a_tool_result_after_the_turn_ended_answers_without_opening_one():
    """The answer to an old call is not a new exchange: the item completes under the id it was
    opened with, and no bracket opens around it."""
    subject = projector()
    subject.admit(_PROMPT_1, after_batch_seq=None)
    opened = subject.observe(
        1, assistant(tool_use_block("toolu_1", "Bash", {"command": "sleep 100"}), message_id="msg_A")
    ).operations
    subject.observe(2, result())

    answered = subject.observe(3, tool_result("toolu_1", "done", structured=BASH_RESULT)).operations

    call = next(operation for operation in opened if isinstance(operation, ItemOpened))
    assert [type(operation) for operation in answered] == [ItemSegment, ItemCompleted]
    assert all(
        operation.item_id == call.item_id
        for operation in answered
        if isinstance(operation, ItemSegment | ItemCompleted)
    )


def test_a_turn_whose_prose_exists_only_on_its_result_frame_mints_the_message():
    """The v3 fold's `close_answer` fallback, ported: no stream, no completed block, and the
    answer's only copy on the terminal frame — the turn mints it as one whole message there. A
    turn that already completed a message mints nothing: the terminal `result` repeats it."""
    subject = projector()
    subject.admit(_PROMPT_1, after_batch_seq=None)

    ended = subject.observe(1, result() | {"result": "re: three"}).operations

    assert [type(operation) for operation in ended] == [ItemOpened, ItemSegment, ItemCompleted, TurnEnded]
    minted = ended[0]
    assert isinstance(minted, ItemOpened)
    assert isinstance(minted.item, MessageOpen)
    segment = ended[1]
    assert isinstance(segment, ItemSegment)
    assert segment.text == "re: three"

    spoke = projector()
    spoke.admit(_PROMPT_1, after_batch_seq=None)
    spoke.observe(1, assistant(text_block("re: four"), message_id="msg_A"))
    repeated = spoke.observe(2, result() | {"result": "re: four"}).operations
    # Only the open message's close and the bracket's end — the prose was already said.
    assert [type(operation) for operation in repeated] == [ItemCompleted, TurnEnded]


def test_a_stray_result_with_no_turn_open_is_counted():
    operations, misses = fold(projector(), [result()])

    assert operations == ()
    assert misses == {"result/no_open_turn": 1}


def test_a_turn_that_could_not_finish_states_its_failure():
    operations, _ = fold(
        projector(), [assistant(text_block("partial"), message_id="msg_A"), result(subtype="error_during_execution")]
    )
    assert [operation.end for operation in operations if isinstance(operation, TurnEnded)] == [
        TurnFailed(failure="error_during_execution: end_turn")
    ]

    bare, _ = fold(
        projector(), [assistant(text_block("x"), message_id="msg_B"), result(subtype="error", stop_reason=None)]
    )
    assert [operation.end for operation in bare if isinstance(operation, TurnEnded)] == [
        TurnFailed(failure="error: unknown error")
    ]


def test_every_did_this_go_wrong_field_is_uninformative():
    """Absent `is_error` is not `is_error: false`: the tri-state maps to UNKNOWN / SUCCEEDED /
    FAILED, and the showable half of each result is segments while the real output is
    `structured`."""
    deferred = {"matches": [{"name": "Bash"}], "total": 112}
    operations, _ = fold(
        projector(),
        [
            assistant(tool_use_block("toolu_1", "Bash", {"command": "true"}), message_id="msg_A"),
            tool_result("toolu_1", "ok", structured=BASH_RESULT),
            assistant(tool_use_block("toolu_2", "Bash", {"command": "true"}), message_id="msg_A"),
            tool_result("toolu_2", "ok", structured=BASH_RESULT, is_error=False),
            assistant(tool_use_block("toolu_3", "Bash", {"command": "false"}), message_id="msg_A"),
            tool_result(
                "toolu_3", [{"type": "text", "text": "No such "}, {"type": "text", "text": "file"}], is_error=True
            ),
            assistant(tool_use_block("toolu_4", "ToolSearch", {"query": "shell"}), message_id="msg_A"),
            tool_result("toolu_4", [{"tool_name": "Bash", "type": "tool_reference"}], structured=deferred),
            assistant(tool_use_block("toolu_5", "Bash", {"command": "true"}), message_id="msg_A"),
            tool_result("toolu_5", "", structured=BASH_RESULT),
        ],
    )

    completions = [
        operation.completion
        for operation in operations
        if isinstance(operation, ItemCompleted) and isinstance(operation.completion, ToolCallCompletion)
    ]
    assert [completion.outcome for completion in completions] == [
        ToolOutcome.UNKNOWN,
        ToolOutcome.SUCCEEDED,
        ToolOutcome.FAILED,
        ToolOutcome.UNKNOWN,
        ToolOutcome.UNKNOWN,
    ]
    assert [completion.structured for completion in completions] == [
        BASH_RESULT,
        BASH_RESULT,
        None,
        deferred,
        BASH_RESULT,
    ]
    segments = [operation.text for operation in operations if isinstance(operation, ItemSegment)]
    # A list of text blocks is still prose; a provider-specific block list renders as its JSON;
    # and a call that printed nothing carries no empty segment.
    assert segments == ["ok", "ok", "No such file", json.dumps([{"tool_name": "Bash", "type": "tool_reference"}])]
    assert_wire_round_trips(operations)


def test_most_of_the_wire_is_ignored_by_name_and_the_rest_is_counted():
    """Deliberately ignored classes are not noise in the signal that says the CLI grew a frame;
    everything else lands in `unprojected`, never a crash and never a silent drop."""
    subject = projector()
    subject.admit(_PROMPT_1, after_batch_seq=None)
    operations, misses = fold(
        subject,
        [
            heartbeat(),
            system("status", status="working"),
            system("init", session_id="a2d5"),
            {"command_uuid": "cmd_1", "state": "started", "type": "command_lifecycle"},
            {"type": "rate_limit_event", "info": {}},
            {"type": "tool_progress", "tool_use_id": "toolu_1", "elapsed_time_seconds": 31},
            {"type": "tool_progress", "tool_use_id": "toolu_1", "elapsed_time_seconds": 62},
            system("vcs_state_changed", cwd="/w", kind="commit"),
            {"type": "telepathy_event", "thought": "…"},
            {
                "isSynthetic": True,
                "message": {"content": [{"text": "No response requested.", "type": "text"}], "role": "user"},
                "type": "user",
            },
        ],
    )

    assert operations == ()
    assert misses == {"tool_progress": 2, "system/vcs_state_changed": 1, "telepathy_event": 1, "user/text": 1}


@pytest.mark.parametrize(
    ("text", "delivered", "expected"),
    [
        pytest.param("Looking at the migration.", "Looking at ", "the migration.", id="mid-stream"),
        pytest.param("Looking at the migration.", "", "Looking at the migration.", id="no-deltas"),
        pytest.param("Looking at the migration.", "Looking at the migration.", "", id="fully-streamed"),
    ],
)
def test_a_completed_block_is_emitted_minus_what_was_already_said(text: str, delivered: str, expected: str):
    assert undelivered(text, delivered) == expected


# --- composed with the journal --------------------------------------------------------------


def test_the_projector_feeds_the_journal_and_admissions_pin_their_frontier():
    """The stage-4 composition, in miniature: every yield is recorded, batches cut per the
    immediate-flush rule, and an admission's `after_batch_seq` is the journal's frontier at the
    injection fence — the last already-numbered batch, whatever later shares the admission's
    batch."""
    subject = projector()
    journal = OperationJournal()

    assert journal.admission_frontier is None
    admitted = subject.admit(_PROMPT_1, after_batch_seq=journal.admission_frontier)
    sent = journal.record(admitted.operations, admitted.unprojected)
    assert [batch.runner_batch_seq for batch in sent] == [1]

    # The ACK is in flight: everything the stream now yields coalesces into the next batch.
    for frame_seq, payload in enumerate(
        [text_delta("Check"), text_delta("ing."), assistant(text_block("Checking."), message_id="msg_A")], start=1
    ):
        projected = subject.observe(frame_seq, payload)
        assert journal.record(projected.operations, projected.unprojected) == ()

    # A second prompt injected at a tool boundary mid-coalesce: the frontier still names batch 1,
    # the last batch that has a number — the pending operations have none yet.
    assert journal.admission_frontier == 1
    folded = subject.admit(_PROMPT_2, after_batch_seq=journal.admission_frontier, frame_seq=4)
    assert journal.record(folded.operations, folded.unprojected) == ()

    released = journal.acked(1)
    assert [batch.runner_batch_seq for batch in released] == [2]
    admissions = [operation for operation in released[0].operations if isinstance(operation, PromptAdmitted)]
    assert [admission.after_batch_seq for admission in admissions] == [1]
    assert RUNNER_TO_CONSOLE.validate_json(released[0].model_dump_json()) == released[0]


# --- the recorded capture -------------------------------------------------------------------

_CAPTURE = "haku/runner/claude/testdata/diverse_session.jsonl"


def _capture_records() -> list[dict[str, Any]]:
    source = Path(_CAPTURE)
    path = source if source.exists() else get_required_path(f"ducktape/{_CAPTURE}")
    return [json.loads(line) for line in path.read_text().splitlines()]


@pytest.fixture(scope="module")
def capture() -> tuple[tuple[Operation, ...], dict[str, int], list[dict[str, Any]]]:
    """The whole capture folded once. A record's index is its `frame_seq`; the one
    `raw_stdout_line` record (CLI prose interleaved with the JSON stream) leaves its index unused,
    as the runner's splitter leaves unparseable lines outside the frame log."""
    records = _capture_records()
    subject = projector()
    operations: list[Operation] = []
    misses: Counter[str] = Counter()
    for frame_seq, record in enumerate(records):
        if "frame" not in record:
            continue
        projected = subject.observe(frame_seq, record["frame"])
        operations.extend(projected.operations)
        misses.update(projected.unprojected)
    return tuple(operations), dict(misses), records


def test_the_recorded_capture_folds_to_the_session_it_recorded(
    capture: tuple[tuple[Operation, ...], dict[str, int], list[dict[str, Any]]],
):
    """Four answers, each reasoned then written then acted on; one wake bracket; a turn that
    ended answered even though a command in it failed — which is why the turn's outcome is read
    off `subtype` and never off a call's."""
    operations, _, _ = capture

    opened_by_type = Counter(operation.item.item_type for operation in operations if isinstance(operation, ItemOpened))
    completed_by_type = Counter(
        operation.completion.item_type for operation in operations if isinstance(operation, ItemCompleted)
    )
    assert opened_by_type == completed_by_type == {ItemType.MESSAGE: 4, ItemType.REASONING: 4, ItemType.TOOL_CALL: 4}

    turns = [operation for operation in operations if isinstance(operation, TurnOpened)]
    assert [type(turn.cause) for turn in turns] == [WakeCause]
    assert [operation.end for operation in operations if isinstance(operation, TurnEnded)] == [TurnAnswered()]
    # Every item belongs to the one bracket, and every segment and completion names an opened item.
    opened_ids = {operation.item_id for operation in operations if isinstance(operation, ItemOpened)}
    assert all(operation.turn_id == turns[0].turn_id for operation in operations if isinstance(operation, ItemOpened))
    assert all(
        operation.item_id in opened_ids
        for operation in operations
        if isinstance(operation, ItemSegment | ItemCompleted)
    )

    assert_wire_round_trips(operations)


def test_the_capture_segments_are_the_sole_text_authority(
    capture: tuple[tuple[Operation, ...], dict[str, int], list[dict[str, Any]]],
):
    """The invariant the vocabulary rests on, checked against the raw wire: each message item's
    concatenated segments equal the completed text blocks of the frames sharing its backend id —
    deltas and completed blocks together delivering every word exactly once — and each reasoning
    item's segments are its `thinking` block verbatim."""
    operations, _, records = capture

    concatenated: dict[UUID, str] = {}
    for operation in operations:
        if isinstance(operation, ItemSegment):
            concatenated[operation.item_id] = concatenated.get(operation.item_id, "") + operation.text

    from_wire: dict[str, str] = {}
    thinking_from_wire: list[str] = []
    for record in records:
        frame = record.get("frame")
        if not frame or frame.get("type") != "assistant":
            continue
        message = frame["message"]
        for block in message.get("content", []):
            if block.get("type") == "text":
                from_wire[message["id"]] = from_wire.get(message["id"], "") + block["text"]
            if block.get("type") == "thinking":
                thinking_from_wire.append(block["thinking"])

    projected_messages = {
        operation.backend_item_id: concatenated.get(operation.item_id, "")
        for operation in operations
        if isinstance(operation, ItemCompleted) and isinstance(operation.completion, MessageCompletion)
    }
    assert projected_messages == from_wire

    reasoning_ids = [
        operation.item_id
        for operation in operations
        if isinstance(operation, ItemOpened) and isinstance(operation.item, ReasoningOpen)
    ]
    assert [concatenated.get(item_id, "") for item_id in reasoning_ids] == thinking_from_wire


def test_the_capture_pairs_results_by_id_and_marks_the_one_failure(
    capture: tuple[tuple[Operation, ...], dict[str, int], list[dict[str, Any]]],
):
    """Both Bash calls were asked before either was answered and the answers came back in the
    reverse order: pairing by position would swap the turn's one error onto the call that
    succeeded."""
    operations, _, _ = capture

    calls = {
        operation.item_id: operation.item
        for operation in operations
        if isinstance(operation, ItemOpened) and isinstance(operation.item, ToolCallOpen)
    }
    outcomes = {
        operation.item_id: operation.completion.outcome
        for operation in operations
        if isinstance(operation, ItemCompleted) and isinstance(operation.completion, ToolCallCompletion)
    }
    assert set(outcomes) == set(calls)
    failed = [item_id for item_id, outcome in outcomes.items() if outcome is ToolOutcome.FAILED]
    assert [calls[item_id].tool_name for item_id in failed] == ["Bash"]


def test_the_capture_unprojected_census_matches_the_reference_fold(
    capture: tuple[tuple[Operation, ...], dict[str, int], list[dict[str, Any]]],
):
    """The drift detector against the Console projector this ports: the same five frame classes
    reach the default branch, and nothing else — a sixth key is the next CLI release adding a
    class, and a missing one is this fold silently swallowing a frame the reference counted."""
    _, misses, _ = capture

    assert misses == {
        "active_goal": 1,
        "autocompact_state": 1,
        "system/commands_changed": 1,
        "system/task_summary": 2,
        "system/post_turn_summary": 1,
    }


if __name__ == "__main__":
    pytest_bazel.main()
