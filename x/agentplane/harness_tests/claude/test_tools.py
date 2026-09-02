"""Tool round trips: what the harness runs, what it reports back upstream, and workspace effects."""

from __future__ import annotations

import pytest_bazel

from x.agentplane.harness_tests.claude import anthropic_sse as sse, frames
from x.agentplane.harness_tests.claude.harness import MODEL, ClaudeHarness
from x.agentplane.harness_tests.claude.requests import MessagesRequest
from x.agentplane.harness_tests.scripted_upstream import ScriptedUpstream
from x.agentplane.native.claude import scenarios

PROBE_FAILURE = (
    'sh -c \'printf "probe stdout before failure\\n"; printf "probe stderr before failure\\n" >&2; exit 23\''
)


def test_parallel_shell_tools_report_both_streams_and_exit_codes(
    claude: ClaudeHarness, upstream: ScriptedUpstream
) -> None:
    with claude.start(upstream) as process:
        scenarios.launch_handshake(process)
        scenarios.send(process, "Use the shell probe and report its outcomes.")

        raw = upstream.next_request()
        upstream.respond(
            raw,
            sse.message_stream(
                [
                    sse.Thinking("run both", "sig_test_1"),
                    sse.ToolUse("toolu_test_1", "Bash", {"command": "printf 'PROBE_STDOUT\\n'"}),
                    sse.ToolUse("toolu_test_2", "Bash", {"command": PROBE_FAILURE}),
                ],
                model=MODEL,
            ),
        )

        raw = upstream.next_request()
        request = MessagesRequest.parse(raw)
        # The thinking block and its signature come back verbatim ahead of the tool uses.
        assert [(block.thinking, block.signature) for block in request.thinking_blocks] == [("run both", "sig_test_1")]
        assert [use.id for use in request.tool_uses] == ["toolu_test_1", "toolu_test_2"]
        first, second = request.tool_results
        assert first.tool_use_id == "toolu_test_1"
        assert first.text.startswith("PROBE_STDOUT")
        assert first.is_error is False
        assert second.tool_use_id == "toolu_test_2"
        assert "probe stdout before failure" in second.text
        assert "probe stderr before failure" in second.text
        assert "23" in second.text
        assert second.is_error is True
        upstream.respond(raw, sse.message_stream([sse.Text("SHELL_PROBE_DONE")], model=MODEL))

        assert scenarios.await_result(process)["result"] == "SHELL_PROBE_DONE"
        assert process.alive()
    captured = process.stdout_frames()
    frames.assert_success(captured, "SHELL_PROBE_DONE")
    results = frames.assert_tool_lifecycles(captured, ["Bash", "Bash"])
    assert any("PROBE_STDOUT" in str(result) for result in results)
    assert any("probe stderr before failure" in str(result) and "23" in str(result) for result in results)
    upstream.assert_quiescent()


def test_file_edit_round_trip_changes_the_workspace(claude: ClaudeHarness, upstream: ScriptedUpstream) -> None:
    editable = claude.workspace / "editable.txt"
    editable.write_text("before\n")
    path = str(editable)
    with claude.start(upstream) as process:
        scenarios.launch_handshake(process)
        scenarios.send(
            process, "Read editable.txt, change it to exactly `after\\n`, reread it, then reply FILE_EDIT_DONE."
        )

        raw = upstream.next_request()
        upstream.respond(
            raw, sse.message_stream([sse.ToolUse("toolu_test_1", "Read", {"file_path": path})], model=MODEL)
        )

        raw = upstream.next_request()
        (read_result,) = MessagesRequest.parse(raw).tool_results
        assert read_result.tool_use_id == "toolu_test_1"
        assert "before" in read_result.text
        upstream.respond(
            raw,
            sse.message_stream(
                [
                    sse.ToolUse(
                        "toolu_test_2", "Edit", {"file_path": path, "old_string": "before", "new_string": "after"}
                    )
                ],
                model=MODEL,
            ),
        )

        raw = upstream.next_request()
        (edit_result,) = MessagesRequest.parse(raw).tool_results
        assert edit_result.tool_use_id == "toolu_test_2"
        assert edit_result.is_error is False
        assert editable.read_text() == "after\n"
        upstream.respond(
            raw, sse.message_stream([sse.ToolUse("toolu_test_3", "Read", {"file_path": path})], model=MODEL)
        )

        raw = upstream.next_request()
        (reread_result,) = MessagesRequest.parse(raw).tool_results
        assert reread_result.tool_use_id == "toolu_test_3"
        assert "after" in reread_result.text
        assert "before" not in reread_result.text
        upstream.respond(raw, sse.message_stream([sse.Text("FILE_EDIT_DONE")], model=MODEL))

        assert scenarios.await_result(process)["result"] == "FILE_EDIT_DONE"
    captured = process.stdout_frames()
    frames.assert_success(captured, "FILE_EDIT_DONE")
    frames.assert_tool_lifecycles(captured, ["Read", "Edit", "Read"])
    upstream.assert_quiescent()


if __name__ == "__main__":
    pytest_bazel.main()
