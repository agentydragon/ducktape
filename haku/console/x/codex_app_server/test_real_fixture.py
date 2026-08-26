from pathlib import Path

import pytest_bazel

from haku.console.chat_models import ItemType, ReasoningDisclosure, ToolOutcome, TurnOutcome
from haku.console.x.codex_app_server.projection import RecordedFrame, project_log
from haku.console.x.codex_app_server.protocol import (
    JsonObject,
    Notification,
    parse_message,
    read_trace,
    server_messages,
)
from haku.console.x.codex_app_server.runtime import CodexRuntimeAdapter
from haku.console.x.conversation_events import (
    CallRef,
    FrameRange,
    ItemSegment,
    MessageCompleted,
    MessageStarted,
    OpenRef,
    ReasoningCompleted,
    ReasoningStarted,
    ToolCallCompleted,
    ToolCallStarted,
    TurnCompleted,
)
from haku.runtime.x.bridge.protocol import HarnessFrame
from util.bazel.runfiles import get_required_path

_TESTDATA = "haku/console/x/codex_app_server/testdata"
_TEXT_COMMAND = f"{_TESTDATA}/real_text_command.sanitized.jsonl"
_PROVIDER_FAILURE = f"{_TESTDATA}/real_provider_failure.sanitized.jsonl"
_MESSAGE = OpenRef(item_type=ItemType.MESSAGE)


def _frames(fixture: str) -> tuple[RecordedFrame, ...]:
    source = Path(fixture)
    path = source if source.exists() else get_required_path(f"ducktape/{fixture}")
    return tuple(RecordedFrame(record.seq, record.message) for record in server_messages(read_trace(path)))


def test_real_capture_projects_both_observed_turn_lifecycles():
    projection = project_log(_frames(_TEXT_COMMAND))

    assert projection.events == (
        MessageStarted(provenance=FrameRange(12, 12)),
        ItemSegment(item=_MESSAGE, text="TRACE", provenance=FrameRange(13, 13)),
        ItemSegment(item=_MESSAGE, text="_TEXT", provenance=FrameRange(14, 14)),
        ItemSegment(item=_MESSAGE, text="_OK", provenance=FrameRange(15, 15)),
        MessageCompleted(backend_item_id="<protocol-id-4>", provenance=FrameRange(12, 16)),
        TurnCompleted(outcome=TurnOutcome.ANSWERED, provenance=FrameRange(17, 17)),
        ReasoningStarted(provenance=FrameRange(23, 23)),
        ReasoningCompleted(disclosure=ReasoningDisclosure.SUMMARY, provenance=FrameRange(23, 24)),
        ToolCallStarted(
            call_id="exec-<protocol-id-8>",
            tool_name="commandExecution",
            arguments={"command": "<ABSOLUTE_PATH> -c 'printf TRACE_CMD_OK'", "cwd": "<WORKSPACE>"},
            provenance=FrameRange(25, 25),
        ),
        ToolCallCompleted(
            item=CallRef(call_id="exec-<protocol-id-8>"),
            structured={
                "command": "<ABSOLUTE_PATH> -c 'printf TRACE_CMD_OK'",
                "cwd": "<WORKSPACE>",
                "processId": "<process-id>",
                "source": "unifiedExecStartup",
                "status": "completed",
                "commandActions": [{"type": "unknown", "command": "printf TRACE_CMD_OK"}],
                "exitCode": 0,
                "durationMs": 0,
            },
            outcome=ToolOutcome.SUCCEEDED,
            provenance=FrameRange(26, 26),
        ),
        MessageStarted(provenance=FrameRange(27, 27)),
        ItemSegment(item=_MESSAGE, text="TRACE", provenance=FrameRange(28, 28)),
        ItemSegment(item=_MESSAGE, text="_COMMAND", provenance=FrameRange(29, 29)),
        ItemSegment(item=_MESSAGE, text="_DONE", provenance=FrameRange(30, 30)),
        MessageCompleted(backend_item_id="<protocol-id-9>", provenance=FrameRange(27, 31)),
        TurnCompleted(outcome=TurnOutcome.ANSWERED, provenance=FrameRange(32, 32)),
    )
    assert projection.unprojected == {}


def _error_params() -> tuple[JsonObject, ...]:
    """The `params` of every `error` notification in the capture, in wire order."""
    parsed = (parse_message(frame.payload) for frame in _frames(_PROVIDER_FAILURE))
    return tuple(
        message.params
        for message in parsed
        if isinstance(message, Notification) and message.method == "error" and message.params is not None
    )


def test_provider_failure_capture_states_its_reason_and_whether_codex_will_retry():
    """`ErrorNotification.willRetry` is the retryability signal; the reason moves field on the last frame.

    Codex retries under `willRetry=true` with the reason in `additionalDetails` and a bare progress
    counter in `message`, then repeats the same `TurnError` under `willRetry=false`, this time with
    the reason in `message`.  A reader that takes `message` alone renders the counter as the failure.
    The counters enumerate Codex's own retry budget, so their count and their `/N` have to agree.
    """
    *retries, terminal = _error_params()

    assert [params["error"]["message"] for params in retries] == [f"Reconnecting... {n}/5" for n in range(1, 6)]
    assert [params["willRetry"] for params in retries] == [True] * len(retries)
    assert terminal["willRetry"] is False
    assert all(params["error"]["additionalDetails"] == terminal["error"]["message"] for params in retries)
    assert terminal["error"]["additionalDetails"] is None


def test_provider_failure_notification_and_terminal_turn_agree_on_one_turn_error():
    """`turn.error` on `turn/completed` repeats the final notification's `TurnError`, categorized."""
    terminal = _error_params()[-1]
    completed = next(frame for frame in _frames(_PROVIDER_FAILURE) if frame.payload.get("method") == "turn/completed")
    turn = completed.payload["params"]["turn"]

    assert turn["status"] == "failed"
    assert turn["error"] == terminal["error"]
    assert turn["error"]["codexErrorInfo"] == "internalServerError"


def test_provider_failure_capture_projects_only_a_bare_failed_outcome():
    """#4752: the projection keeps the outcome and drops every durable trace of the reason."""
    projected = project_log(_frames(_PROVIDER_FAILURE))

    assert projected.events == (TurnCompleted(outcome=TurnOutcome.FAILED, provenance=FrameRange(27, 27)),)
    assert projected.unprojected["error"] == len(_error_params())


def test_provider_failure_reason_survives_only_as_far_as_the_transient_completion():
    """The adapter does read the reason off `turn.error`; no durable conversation event can hold it."""
    completed = next(frame for frame in _frames(_PROVIDER_FAILURE) if frame.payload.get("method") == "turn/completed")

    effects = (
        CodexRuntimeAdapter()
        .turn_handler()
        .apply(frame_seq=completed.frame_seq, frame=HarnessFrame(frame=completed.payload))
    )

    assert effects.completion is not None
    assert (
        effects.completion.failure
        == f"the agent's turn failed: {completed.payload['params']['turn']['error']['message']}"
    )
    assert effects.events == (TurnCompleted(outcome=TurnOutcome.FAILED, provenance=FrameRange(27, 27)),)


if __name__ == "__main__":
    pytest_bazel.main()
