"""Tool round trips: what the harness runs, what it reports back upstream, and workspace effects."""

from __future__ import annotations

import asyncio

import pytest
import pytest_bazel

from agentplane.harness_tests.claude import anthropic_sse, frames
from agentplane.harness_tests.claude.harness import MODEL, ClaudeLauncher
from agentplane.harness_tests.claude.messages import AnthropicMessages

PROBE_FAILURE = (
    'sh -c \'printf "probe stdout before failure\\n"; printf "probe stderr before failure\\n" >&2; exit 23\''
)


async def test_parallel_shell_tools_report_both_streams_and_exit_codes(
    claude: ClaudeLauncher, anthropic_messages: AnthropicMessages
) -> None:
    async with claude.start(anthropic_messages) as run:
        prompt = await run.send("Use the shell probe and report its outcomes.")

        async with await anthropic_messages.await_next_request() as exchange:
            stream = anthropic_sse.message_stream(
                [
                    anthropic_sse.Thinking("run both", "sig_test_1"),
                    anthropic_sse.ToolUse("toolu_test_1", "Bash", {"command": "printf 'PROBE_STDOUT\\n'"}),
                    anthropic_sse.ToolUse("toolu_test_2", "Bash", {"command": PROBE_FAILURE}),
                ],
                model=MODEL,
            )
            await exchange.send(*stream.events)

        async with await anthropic_messages.await_next_request() as exchange:
            request = exchange.request
            # The thinking block and its signature come back verbatim ahead of the tool uses.
            assert [(block.thinking, block.signature) for block in request.thinking_blocks] == [
                ("run both", "sig_test_1")
            ]
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
            stream = anthropic_sse.message_stream([anthropic_sse.Text("SHELL_PROBE_DONE")], model=MODEL)
            await exchange.send(*stream.events)

        assert (await prompt.result()).result == "SHELL_PROBE_DONE"
        assert run.running
    captured = run.native_frames()
    frames.assert_success(captured, "SHELL_PROBE_DONE")
    results = frames.assert_tool_lifecycles(captured, ["Bash", "Bash"])
    assert any("PROBE_STDOUT" in str(result) for result in results)
    assert any("probe stderr before failure" in str(result) and "23" in str(result) for result in results)


async def test_file_edit_round_trip_changes_the_workspace(
    claude: ClaudeLauncher, anthropic_messages: AnthropicMessages
) -> None:
    editable = claude.workspace / "editable.txt"
    editable.write_text("before\n")
    path = str(editable)
    async with claude.start(anthropic_messages) as run:
        prompt = await run.send(
            "Read editable.txt, change it to exactly `after\\n`, reread it, then reply FILE_EDIT_DONE."
        )

        async with await anthropic_messages.await_next_request() as exchange:
            stream = anthropic_sse.message_stream(
                [anthropic_sse.ToolUse("toolu_test_1", "Read", {"file_path": path})], model=MODEL
            )
            await exchange.send(*stream.events)

        async with await anthropic_messages.await_next_request() as exchange:
            (read_result,) = exchange.request.tool_results
            assert read_result.tool_use_id == "toolu_test_1"
            assert "before" in read_result.text
            stream = anthropic_sse.message_stream(
                [
                    anthropic_sse.ToolUse(
                        "toolu_test_2", "Edit", {"file_path": path, "old_string": "before", "new_string": "after"}
                    )
                ],
                model=MODEL,
            )
            await exchange.send(*stream.events)

        async with await anthropic_messages.await_next_request() as exchange:
            (edit_result,) = exchange.request.tool_results
            assert edit_result.tool_use_id == "toolu_test_2"
            assert edit_result.is_error is False
            assert editable.read_text() == "after\n"
            stream = anthropic_sse.message_stream(
                [anthropic_sse.ToolUse("toolu_test_3", "Read", {"file_path": path})], model=MODEL
            )
            await exchange.send(*stream.events)

        async with await anthropic_messages.await_next_request() as exchange:
            (reread_result,) = exchange.request.tool_results
            assert reread_result.tool_use_id == "toolu_test_3"
            assert "after" in reread_result.text
            assert "before" not in reread_result.text
            stream = anthropic_sse.message_stream([anthropic_sse.Text("FILE_EDIT_DONE")], model=MODEL)
            await exchange.send(*stream.events)

        assert (await prompt.result()).result == "FILE_EDIT_DONE"
    captured = run.native_frames()
    frames.assert_success(captured, "FILE_EDIT_DONE")
    frames.assert_tool_lifecycles(captured, ["Read", "Edit", "Read"])


async def test_subagent_tool_frames_are_correlated_with_the_parent_call(
    claude: ClaudeLauncher, anthropic_messages: AnthropicMessages
) -> None:
    """The pinned Agent launches asynchronously; parent completion does not finish the child."""
    async with claude.start(anthropic_messages, subagents=True) as run:
        async with asyncio.timeout(45):
            prompt = await run.send("Delegate the shell probe to a child, then report its result.")

            async with await anthropic_messages.await_next_request() as exchange:
                assert "Agent" in exchange.request.tool_names
                stream = anthropic_sse.message_stream(
                    [
                        anthropic_sse.ToolUse(
                            "toolu_spawn_child",
                            "Agent",
                            {
                                "description": "Child shell probe",
                                "subagent_type": "general-purpose",
                                "prompt": "Run the child shell probe and reply CHILD_DONE.",
                            },
                        )
                    ],
                    model=MODEL,
                )
                await exchange.send(*stream.events)

            # The parent gets an async-launch result independently of the child's first request.
            # Hold the child at the model boundary until the parent has finished its initial turn.
            async with (
                await anthropic_messages.await_next_request() as first,
                await anthropic_messages.await_next_request() as second,
            ):
                parent, child = (first, second) if first.request.tool_results else (second, first)
                (launched,) = parent.request.tool_results
                assert launched.tool_use_id == "toolu_spawn_child"
                assert launched.is_error is False
                assert "Async agent launched successfully" in launched.text
                assert "Run the child shell probe" in "\n".join(child.request.texts("user"))
                assert not child.request.tool_results
                assert "Bash" in child.request.tool_names
                stream = anthropic_sse.message_stream([anthropic_sse.Text("PARENT_WAITING")], model=MODEL)
                await parent.send(*stream.events)
                await parent.close()
                assert (await prompt.result()).result == "PARENT_WAITING"
                completion = run.events()
                stream = anthropic_sse.message_stream(
                    [anthropic_sse.ToolUse("toolu_child_shell", "Bash", {"command": "printf CHILD_TOOL_OUTPUT"})],
                    model=MODEL,
                )
                await child.send(*stream.events)

            async with await anthropic_messages.await_next_request() as exchange:
                (result,) = exchange.request.tool_results
                assert result.tool_use_id == "toolu_child_shell"
                assert result.is_error is False
                assert "CHILD_TOOL_OUTPUT" in result.text
                stream = anthropic_sse.message_stream([anthropic_sse.Text("CHILD_DONE")], model=MODEL)
                await exchange.send(*stream.events)

            # Completion arrives as a new parent input, not as the earlier Agent tool result.
            async with await anthropic_messages.await_next_request() as exchange:
                assert "CHILD_DONE" in "\n".join(exchange.request.texts("user"))
                stream = anthropic_sse.message_stream([anthropic_sse.Text("PARENT_DONE")], model=MODEL)
                await exchange.send(*stream.events)

            assert (await completion.result()).result == "PARENT_DONE"
            assert run.running

    captured = run.native_frames()
    frames.assert_success(captured, "PARENT_DONE")
    child_tool_frames = [
        frame
        for frame in captured
        if frame.get("type") == "assistant"
        and any(block.get("id") == "toolu_child_shell" for block in frame["message"]["content"])
    ]
    assert child_tool_frames
    assert {frame["parent_tool_use_id"] for frame in child_tool_frames} == {"toolu_spawn_child"}
    started = [frame for frame in captured if frame.get("subtype") == "task_started"]
    (task,) = started
    assert task["tool_use_id"] == "toolu_spawn_child"
    assert task["is_backgrounded"] is True
    launch_results = [
        result for result in frames.tool_results(captured) if isinstance(result, dict) and "agentId" in result
    ]
    (launch_result,) = launch_results
    assert launch_result["agentId"] == task["task_id"]
    assert launch_result["isAsync"] is True
    notifications = [frame for frame in captured if frame.get("subtype") == "task_notification"]
    (notification,) = notifications
    assert notification["task_id"] == task["task_id"]
    assert notification["tool_use_id"] == "toolu_spawn_child"
    assert notification["status"] == "completed"
    # 2.1.252 forwards completed child prose even without an explicit forwarding opt-in.
    child_text_frames = [
        frame
        for frame in captured
        if frame.get("type") == "assistant"
        and any(block.get("text") == "CHILD_DONE" for block in frame["message"]["content"])
    ]
    (child_text,) = child_text_frames
    assert child_text["parent_tool_use_id"] == "toolu_spawn_child"
    assert child_text["session_id"] == notification["session_id"]


@pytest.mark.parametrize("resume_parent", [False, True])
async def test_send_message_resumes_a_completed_child_and_task_output_reads_its_result(
    claude: ClaudeLauncher, anthropic_messages: AnthropicMessages, resume_parent: bool
) -> None:
    """A SendMessage receipt is not child completion; TaskOutput reads the later result."""
    async with claude.start(anthropic_messages, subagents=True) as run:
        async with asyncio.timeout(45):
            prompt = await run.send("Delegate a probe, then send the same child a follow-up.")
            async with await anthropic_messages.await_next_request() as exchange:
                assert {"Agent", "SendMessage", "TaskOutput"} <= set(exchange.request.tool_names)
                stream = anthropic_sse.message_stream(
                    [
                        anthropic_sse.ToolUse(
                            "toolu_spawn_message_child",
                            "Agent",
                            {
                                "description": "Messaging probe",
                                "subagent_type": "general-purpose",
                                "prompt": "Reply CHILD_FIRST_DONE.",
                            },
                        )
                    ],
                    model=MODEL,
                )
                await exchange.send(*stream.events)

            async with (
                await anthropic_messages.await_next_request() as first,
                await anthropic_messages.await_next_request() as second,
            ):
                parent, child = (first, second) if first.request.tool_results else (second, first)
                (launched,) = parent.request.tool_results
                assert launched.tool_use_id == "toolu_spawn_message_child"
                assert launched.is_error is False
                assert "Reply CHILD_FIRST_DONE." in "\n".join(child.request.texts("user"))
                launch_results = [
                    result
                    for result in frames.tool_results(run.native_frames())
                    if isinstance(result, dict) and "agentId" in result
                ]
                (launch_result,) = launch_results
                agent_id = launch_result["agentId"]
                stream = anthropic_sse.message_stream([anthropic_sse.Text("PARENT_WAITING")], model=MODEL)
                await parent.send(*stream.events)
                await parent.close()
                assert (await prompt.result()).result == "PARENT_WAITING"
                followup = run.events()
                stream = anthropic_sse.message_stream([anthropic_sse.Text("CHILD_FIRST_DONE")], model=MODEL)
                await child.send(*stream.events)

            async with await anthropic_messages.await_next_request() as exchange:
                assert "CHILD_FIRST_DONE" in "\n".join(exchange.request.texts("user"))
                stream = anthropic_sse.message_stream(
                    [
                        anthropic_sse.ToolUse(
                            "toolu_followup",
                            "SendMessage",
                            {
                                "to": agent_id,
                                "message": "FOLLOWUP_PROBE: reply CHILD_SECOND_DONE.",
                                "summary": "Follow up on the completed probe",
                            },
                        )
                    ],
                    model=MODEL,
                )
                await exchange.send(*stream.events)

            async with (
                await anthropic_messages.await_next_request() as first,
                await anthropic_messages.await_next_request() as second,
            ):
                parent, child = (first, second) if first.request.tool_results else (second, first)
                (sent,) = parent.request.tool_results
                assert sent.tool_use_id == "toolu_followup"
                assert sent.is_error is False
                # Prove continuity of the child's transcript as well as delivery of the message.
                assert "CHILD_FIRST_DONE" in child.request.texts("assistant")
                assert "FOLLOWUP_PROBE" in "\n".join(child.request.texts("user"))
                stream = anthropic_sse.message_stream([anthropic_sse.Text("PARENT_FOLLOWUP_WAITING")], model=MODEL)
                await parent.send(*stream.events)
                await parent.close()
                assert (await followup.result()).result == "PARENT_FOLLOWUP_WAITING"
                completion = run.events()
                stream = anthropic_sse.message_stream([anthropic_sse.Text("CHILD_SECOND_DONE")], model=MODEL)
                await child.send(*stream.events)

            async with await anthropic_messages.await_next_request() as exchange:
                assert "CHILD_SECOND_DONE" in "\n".join(exchange.request.texts("user"))
                stream = anthropic_sse.message_stream(
                    [anthropic_sse.ToolUse("toolu_read_child", "TaskOutput", {"task_id": agent_id, "block": False})],
                    model=MODEL,
                )
                await exchange.send(*stream.events)

            async with await anthropic_messages.await_next_request() as exchange:
                (read,) = exchange.request.tool_results
                assert read.tool_use_id == "toolu_read_child"
                assert read.is_error is False
                assert "CHILD_SECOND_DONE" in read.text
                stream = anthropic_sse.message_stream([anthropic_sse.Text("PARENT_DONE")], model=MODEL)
                await exchange.send(*stream.events)
            assert (await completion.result()).result == "PARENT_DONE"

    captured = run.native_frames()
    frames.assert_success(captured, "PARENT_DONE")
    notifications = [frame for frame in captured if frame.get("subtype") == "task_notification"]
    assert len(notifications) == 2
    assert {frame["task_id"] for frame in notifications} == {agent_id}
    assert {frame["status"] for frame in notifications} == {"completed"}
    results = [result for result in frames.tool_results(captured) if isinstance(result, dict)]
    sent_results = [result for result in results if "resumedAgentId" in result]
    (sent_result,) = sent_results
    assert sent_result["success"] is True
    assert sent_result["resumedAgentId"] == agent_id
    read_results = [result for result in results if "retrieval_status" in result]
    (read_result,) = read_results
    assert read_result["retrieval_status"] == "success"
    assert read_result["task"]["task_id"] == agent_id
    assert read_result["task"]["status"] == "completed"

    if resume_parent:
        session_id = next(frame["session_id"] for frame in captured if frame.get("type") == "result")
        async with claude.start(anthropic_messages, subagents=True, resume_id=session_id) as resumed:
            async with asyncio.timeout(45):
                recovery = await resumed.send("Read the earlier child's result without restarting it.")
                async with await anthropic_messages.await_next_request() as exchange:
                    assert "CHILD_SECOND_DONE" in "\n".join(exchange.request.texts("user"))
                    await exchange.send(
                        *anthropic_sse.message_stream(
                            [
                                anthropic_sse.ToolUse(
                                    "toolu_read_after_resume", "TaskOutput", {"task_id": agent_id, "block": False}
                                )
                            ],
                            model=MODEL,
                        ).events
                    )
                async with await anthropic_messages.await_next_request() as exchange:
                    (read,) = exchange.request.tool_results
                    assert read.tool_use_id == "toolu_read_after_resume"
                    assert read.is_error is True, read
                    assert agent_id in read.text
                    await exchange.send(
                        *anthropic_sse.message_stream([anthropic_sse.Text("RESUME_PROBE_DONE")], model=MODEL).events
                    )
                assert (await recovery.result()).result == "RESUME_PROBE_DONE"
                assert not [
                    frame
                    for frame in resumed.native_frames()[len(captured) :]
                    if frame.get("subtype") == "task_notification"
                ]


if __name__ == "__main__":
    pytest_bazel.main()
