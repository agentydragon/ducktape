"""Tool round trips and `multi_agent_v1` child spawn/wait behavior.

The parent and child model requests can interleave; this scenario routes by the native thread ID,
checks the spawn result's child identity, and waits for a separately completed child result.
After the parent resumes, its history still contains the child answer, but `wait_agent` reports the
old child as `notFound`; this does not resume or rediscover the child.
"""

from __future__ import annotations

import asyncio
import json
import shlex

import pytest
import pytest_bazel

from agentplane.native.codex import scenarios, wire
from agentplane.native.codex.testing import frames, responses_sse
from agentplane.native.codex.testing.harness import CodexHarness
from agentplane.native.codex.testing.model import MODEL
from agentplane.native.codex.testing.responses import OpenAIResponses

PROBE_FAILURE = (
    'sh -c \'printf "probe stdout before failure\\n"; printf "probe stderr before failure\\n" >&2; exit 23\''
)


async def test_parallel_shell_commands_report_output_and_exit_codes(
    codex: CodexHarness, openai_responses: OpenAIResponses
) -> None:
    async with codex.start(openai_responses) as run:
        turn = await run.start_turn("Use the shell probe and report its outcomes.")

        async with await openai_responses.await_next_request() as exchange:
            stream = responses_sse.response_stream(
                [
                    responses_sse.Reasoning("run both", "enc_test_1"),
                    responses_sse.FunctionCall("call_test_1", "exec_command", {"cmd": "printf 'PROBE_STDOUT\\n'"}),
                    responses_sse.FunctionCall("call_test_2", "exec_command", {"cmd": PROBE_FAILURE}),
                ],
                model=MODEL,
            )
            await exchange.send(*stream.events)

        async with await openai_responses.await_next_request() as exchange:
            request = exchange.request
            assert request.item_kinds == [
                "message:developer",
                "message:user",
                "reasoning",
                "function_call",
                "function_call",
                "function_call_output",
                "function_call_output",
            ]
            assert [message.text for message in request.messages("developer")] == [scenarios.BASE_INSTRUCTIONS]
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
            stream = responses_sse.response_stream([responses_sse.Message("SHELL_PROBE_DONE")], model=MODEL)
            await exchange.send(*stream.events)

        assert (await turn.completed()).params.turn.status is wire.TurnStatus.COMPLETED
        assert run.running
    captured = run.native_frames()
    frames.assert_success(captured, "SHELL_PROBE_DONE")
    commands = frames.assert_item_lifecycles(captured, wire.CommandExecutionItem)
    assert len(commands) == 2
    assert any(item.status is wire.CommandExecutionStatus.COMPLETED and item.exit_code == 0 for item in commands), (
        commands
    )
    assert any(item.status is wire.CommandExecutionStatus.FAILED and item.exit_code == 23 for item in commands), (
        commands
    )
    assert any("PROBE_STDOUT" in (item.aggregated_output or "") for item in commands), commands


async def test_file_edit_round_trip_changes_the_workspace(
    codex: CodexHarness, openai_responses: OpenAIResponses
) -> None:
    editable = codex.workspace / "editable.txt"
    editable.write_text("before\n")
    async with codex.start(openai_responses) as run:
        turn = await run.start_turn(
            "Read editable.txt, change it to exactly `after\\n`, reread it, then reply FILE_EDIT_DONE."
        )

        async with await openai_responses.await_next_request() as exchange:
            stream = responses_sse.response_stream(
                [responses_sse.FunctionCall("call_test_1", "exec_command", {"cmd": "cat editable.txt"})], model=MODEL
            )
            await exchange.send(*stream.events)

        async with await openai_responses.await_next_request() as exchange:
            (read_output,) = exchange.request.function_call_outputs
            assert read_output.call_id == "call_test_1"
            assert "before" in read_output.output
            stream = responses_sse.response_stream(
                [
                    responses_sse.FunctionCall(
                        "call_test_2", "exec_command", {"cmd": "printf 'after\\n' > editable.txt"}
                    )
                ],
                model=MODEL,
            )
            await exchange.send(*stream.events)

        async with await openai_responses.await_next_request() as exchange:
            (write_output,) = exchange.request.function_call_outputs[-1:]
            assert write_output.call_id == "call_test_2"
            assert "exited with code 0" in write_output.output
            assert editable.read_text() == "after\n"
            stream = responses_sse.response_stream(
                [responses_sse.FunctionCall("call_test_3", "exec_command", {"cmd": "cat editable.txt"})], model=MODEL
            )
            await exchange.send(*stream.events)

        async with await openai_responses.await_next_request() as exchange:
            (reread_output,) = exchange.request.function_call_outputs[-1:]
            assert reread_output.call_id == "call_test_3"
            assert "after" in reread_output.output
            assert "before" not in reread_output.output
            stream = responses_sse.response_stream([responses_sse.Message("FILE_EDIT_DONE")], model=MODEL)
            await exchange.send(*stream.events)

        assert (await turn.completed()).params.turn.status is wire.TurnStatus.COMPLETED
    captured = run.native_frames()
    frames.assert_success(captured, "FILE_EDIT_DONE")
    commands = frames.assert_item_lifecycles(captured, wire.CommandExecutionItem)
    # Codex wraps each exec_command in a login shell.
    assert [shlex.split(item.command)[-1] for item in commands] == [
        "cat editable.txt",
        "printf 'after\\n' > editable.txt",
        "cat editable.txt",
    ]


@pytest.mark.parametrize("resume_parent", [False, True])
async def test_subagent_spawn_and_wait_report_the_child_identity(
    codex: CodexHarness, openai_responses: OpenAIResponses, resume_parent: bool
) -> None:
    """Parent and child requests may interleave; route by native thread ID, not arrival order."""
    config: dict[str, object] = {"features.multi_agent": True, "features.multi_agent_v2": False}
    async with codex.start(openai_responses, config=config, persist=resume_parent) as run:
        turn = await run.start_turn("Delegate a shell probe to a child and wait for its result.")
        async with await openai_responses.await_next_request() as exchange:
            assert "multi_agent_v1" in exchange.request.tool_names
            stream = responses_sse.response_stream(
                [
                    responses_sse.FunctionCall(
                        "call_spawn_child",
                        "spawn_agent",
                        {"message": "Run the child shell probe and reply CHILD_DONE.", "fork_context": False},
                        namespace="multi_agent_v1",
                    )
                ],
                model=MODEL,
            )
            await exchange.send(*stream.events)

        spawned_id = None
        child_thread_ids: set[str] = set()
        handled: set[str] = set()
        # Exactly two model requests per participant: spawn -> wait -> answer for the parent,
        # and shell -> answer for the child. Holding wait open cannot block answering the child.
        for _ in range(4):
            async with await openai_responses.await_next_request() as exchange:
                request = exchange.request
                if request.client_metadata.thread_id == turn.thread_id:
                    result = request.function_call_outputs[-1]
                    assert result.call_id not in handled
                    handled.add(result.call_id)
                    if result.call_id == "call_spawn_child":
                        spawned_id = json.loads(result.text)["agent_id"]
                        assert spawned_id != turn.thread_id
                        stream = responses_sse.response_stream(
                            [
                                responses_sse.FunctionCall(
                                    "call_wait_child",
                                    "wait_agent",
                                    {"targets": [spawned_id]},
                                    namespace="multi_agent_v1",
                                )
                            ],
                            model=MODEL,
                        )
                    else:
                        assert result.call_id == "call_wait_child"
                        waited = json.loads(result.text)
                        assert waited["timed_out"] is False
                        assert waited["status"] == {spawned_id: {"completed": "CHILD_DONE"}}
                        stream = responses_sse.response_stream([responses_sse.Message("PARENT_DONE")], model=MODEL)
                else:
                    child_thread_ids.add(request.client_metadata.thread_id)
                    if not request.function_call_outputs:
                        assert "child_start" not in handled
                        handled.add("child_start")
                        assert "Run the child shell probe" in "\n".join(
                            message.text for message in request.messages("user")
                        )
                        stream = responses_sse.response_stream(
                            [
                                responses_sse.FunctionCall(
                                    "call_child_shell", "exec_command", {"cmd": "printf CHILD_TOOL_OUTPUT"}
                                )
                            ],
                            model=MODEL,
                        )
                    else:
                        (result,) = request.function_call_outputs
                        assert result.call_id == "call_child_shell"
                        assert result.call_id not in handled
                        handled.add(result.call_id)
                        assert "CHILD_TOOL_OUTPUT" in result.output
                        stream = responses_sse.response_stream([responses_sse.Message("CHILD_DONE")], model=MODEL)
                await exchange.send(*stream.events)

        assert handled == {"call_spawn_child", "call_wait_child", "child_start", "call_child_shell"}
        assert child_thread_ids == {spawned_id}
        # Codex tells the parent about the child's completion with a user message of its own. When that
        # lands after the parent's final answer, the parent takes one more model request in the same
        # turn, and the turn completes only once it is answered.
        turn_done = asyncio.ensure_future(turn.completed())
        while True:
            late_request = asyncio.ensure_future(openai_responses.await_next_request())
            await asyncio.wait({turn_done, late_request}, return_when=asyncio.FIRST_COMPLETED)
            if not late_request.done():
                late_request.cancel()
                break
            async with late_request.result() as exchange:
                assert exchange.request.client_metadata.thread_id == turn.thread_id
                assert any("subagent_notification" in message.text for message in exchange.request.messages("user"))
                await exchange.send(
                    *responses_sse.response_stream([responses_sse.Message("NOTIFICATION_SEEN")], model=MODEL).events
                )
        assert (await turn_done).params.turn.status is wire.TurnStatus.COMPLETED
        assert run.running

    captured = run.native_frames()
    completed = [
        frame["params"]["item"]
        for frame in captured
        if frame.get("method") == "item/completed" and frame["params"]["item"]["type"] == "collabAgentToolCall"
    ]
    assert {item["id"] for item in completed} == {"call_spawn_child", "call_wait_child"}
    started_ids = {
        frame["params"]["item"]["id"]
        for frame in captured
        if frame.get("method") == "item/started" and frame["params"]["item"]["type"] == "collabAgentToolCall"
    }
    assert started_ids == {"call_spawn_child", "call_wait_child"}
    for item in completed:
        assert item["status"] == "completed"
        assert item["senderThreadId"] == turn.thread_id
        assert item["receiverThreadIds"] == [spawned_id]
    wait_item = next(item for item in completed if item["id"] == "call_wait_child")
    assert wait_item["agentsStates"][spawned_id] == {"status": "completed", "message": "CHILD_DONE"}

    if resume_parent:
        async with codex.start(openai_responses, resume_thread_id=run.thread_id) as resumed:
            recovery = await resumed.start_turn("Query the earlier child's status without restarting it.")
            async with await openai_responses.await_next_request() as exchange:
                assert any("CHILD_DONE" in output.output for output in exchange.request.function_call_outputs)
                await exchange.send(
                    *responses_sse.response_stream(
                        [
                            responses_sse.FunctionCall(
                                "call_wait_after_resume",
                                "wait_agent",
                                {"targets": [spawned_id], "timeout_ms": 1000},
                                namespace="multi_agent_v1",
                            )
                        ],
                        model=MODEL,
                    ).events
                )
            async with await openai_responses.await_next_request() as exchange:
                result = exchange.request.function_call_outputs[-1]
                assert result.call_id == "call_wait_after_resume"
                assert json.loads(result.text)["status"] == {spawned_id: "not_found"}, result
                await exchange.send(
                    *responses_sse.response_stream([responses_sse.Message("RESUME_PROBE_DONE")], model=MODEL).events
                )
            assert (await recovery.completed()).params.turn.status is wire.TurnStatus.COMPLETED
        recovered_waits = [
            frame["params"]["item"]
            for frame in resumed.native_frames()
            if frame.get("method") == "item/completed" and frame["params"]["item"]["id"] == "call_wait_after_resume"
        ]
        (recovered_wait,) = recovered_waits
        assert recovered_wait["receiverThreadIds"] == [spawned_id]
        assert recovered_wait["agentsStates"][spawned_id]["status"] == "notFound"


if __name__ == "__main__":
    pytest_bazel.main()
