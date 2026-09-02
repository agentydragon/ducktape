"""Tool round trips: what the harness runs, what it reports back upstream, and workspace effects."""

from __future__ import annotations

import shlex

import pytest_bazel

from x.agentplane.harness_tests.codex import frames, responses_sse as sse
from x.agentplane.harness_tests.codex.harness import EFFORT, MODEL, CodexHarness
from x.agentplane.harness_tests.codex.requests import ResponsesRequest
from x.agentplane.harness_tests.scripted_upstream import ScriptedUpstream
from x.agentplane.native.codex import scenarios

PROBE_FAILURE = (
    'sh -c \'printf "probe stdout before failure\\n"; printf "probe stderr before failure\\n" >&2; exit 23\''
)


def test_parallel_shell_commands_report_output_and_exit_codes(codex: CodexHarness, upstream: ScriptedUpstream) -> None:
    with codex.start(upstream) as process:
        thread_id = scenarios.launch_handshake(process, cwd=str(codex.workspace), model=MODEL, effort=EFFORT)[
            "thread_id"
        ]
        scenarios.start_turn(
            process, thread_id=thread_id, request_id="capture-3", text="Use the shell probe and report its outcomes."
        )

        raw = upstream.next_request()
        upstream.respond(
            raw,
            sse.response_stream(
                [
                    sse.Reasoning("run both", "enc_test_1"),
                    sse.FunctionCall("call_test_1", "exec_command", {"cmd": "printf 'PROBE_STDOUT\\n'"}),
                    sse.FunctionCall("call_test_2", "exec_command", {"cmd": PROBE_FAILURE}),
                ],
                model=MODEL,
            ),
        )

        raw = upstream.next_request()
        request = ResponsesRequest.parse(raw)
        assert request.item_kinds == [
            "message:user",
            "reasoning",
            "function_call",
            "function_call",
            "function_call_output",
            "function_call_output",
        ]
        # The reasoning item's encrypted content comes back verbatim ahead of the calls.
        assert request.reasoning[0].encrypted_content == "enc_test_1"
        assert [call.call_id for call in request.function_calls] == ["call_test_1", "call_test_2"]
        first, second = request.function_call_outputs
        assert first.call_id == "call_test_1"
        assert "PROBE_STDOUT" in first.output
        assert "exited with code 0" in first.output
        assert second.call_id == "call_test_2"
        assert "probe stdout before failure" in second.output
        assert "probe stderr before failure" in second.output
        assert "exited with code 23" in second.output
        upstream.respond(raw, sse.response_stream([sse.Message("SHELL_PROBE_DONE")], model=MODEL))

        assert scenarios.await_turn_completed(process)["params"]["turn"]["status"] == "completed"
        assert process.alive()
    captured = process.stdout_frames()
    frames.assert_success(captured, "SHELL_PROBE_DONE")
    commands = frames.assert_item_lifecycles(captured, "commandExecution")
    assert len(commands) == 2
    assert any(item.get("status") == "completed" and item.get("exitCode") == 0 for item in commands), commands
    assert any(item.get("status") == "failed" and item.get("exitCode") == 23 for item in commands), commands
    assert any("PROBE_STDOUT" in item.get("aggregatedOutput", "") for item in commands), commands
    upstream.assert_quiescent()


def test_file_edit_round_trip_changes_the_workspace(codex: CodexHarness, upstream: ScriptedUpstream) -> None:
    editable = codex.workspace / "editable.txt"
    editable.write_text("before\n")
    with codex.start(upstream) as process:
        thread_id = scenarios.launch_handshake(process, cwd=str(codex.workspace), model=MODEL, effort=EFFORT)[
            "thread_id"
        ]
        scenarios.start_turn(
            process,
            thread_id=thread_id,
            request_id="capture-3",
            text="Read editable.txt, change it to exactly `after\\n`, reread it, then reply FILE_EDIT_DONE.",
        )

        raw = upstream.next_request()
        upstream.respond(
            raw,
            sse.response_stream(
                [sse.FunctionCall("call_test_1", "exec_command", {"cmd": "cat editable.txt"})], model=MODEL
            ),
        )

        raw = upstream.next_request()
        (read_output,) = ResponsesRequest.parse(raw).function_call_outputs
        assert read_output.call_id == "call_test_1"
        assert "before" in read_output.output
        upstream.respond(
            raw,
            sse.response_stream(
                [sse.FunctionCall("call_test_2", "exec_command", {"cmd": "printf 'after\\n' > editable.txt"})],
                model=MODEL,
            ),
        )

        raw = upstream.next_request()
        (write_output,) = ResponsesRequest.parse(raw).function_call_outputs[-1:]
        assert write_output.call_id == "call_test_2"
        assert "exited with code 0" in write_output.output
        assert editable.read_text() == "after\n"
        upstream.respond(
            raw,
            sse.response_stream(
                [sse.FunctionCall("call_test_3", "exec_command", {"cmd": "cat editable.txt"})], model=MODEL
            ),
        )

        raw = upstream.next_request()
        (reread_output,) = ResponsesRequest.parse(raw).function_call_outputs[-1:]
        assert reread_output.call_id == "call_test_3"
        assert "after" in reread_output.output
        assert "before" not in reread_output.output
        upstream.respond(raw, sse.response_stream([sse.Message("FILE_EDIT_DONE")], model=MODEL))

        assert scenarios.await_turn_completed(process)["params"]["turn"]["status"] == "completed"
    captured = process.stdout_frames()
    frames.assert_success(captured, "FILE_EDIT_DONE")
    commands = frames.assert_item_lifecycles(captured, "commandExecution")
    # Codex wraps each exec_command in a login shell.
    assert [shlex.split(item["command"])[-1] for item in commands] == [
        "cat editable.txt",
        "printf 'after\\n' > editable.txt",
        "cat editable.txt",
    ]
    upstream.assert_quiescent()


if __name__ == "__main__":
    pytest_bazel.main()
