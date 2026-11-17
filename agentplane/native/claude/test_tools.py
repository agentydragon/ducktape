"""Tool round trips, including Claude's asynchronous `Agent`/`Task` child lifecycle.

Child tool frames correlate to the parent Agent call, while child completion is a separate native
notification. The `task_started` ID matches the `Agent` result's `agentId`. A follow-up `SendMessage`
resumes the same child; after parent-session resume the conversation transcript remains, but the
old child task is no longer available to `TaskOutput` and no new completion notification arrives.
"""

from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack

import pytest

from agentplane.native.claude.scenarios import MAX_RETRIES
from agentplane.native.claude.testing import anthropic_sse, frames
from agentplane.native.claude.testing.harness import ClaudeLauncher
from agentplane.native.claude.testing.messages import AnthropicMessages
from agentplane.native.claude.testing.model import MODEL
from agentplane.native.testing.async_process import AsyncNativeProcess, FrameCursor

PROBE_FAILURE = (
    'sh -c \'printf "probe stdout before failure\\n"; printf "probe stderr before failure\\n" >&2; exit 23\''
)


async def wait_for_child_delta(cursor: FrameCursor, parent_call: str) -> None:
    while True:
        frame = await cursor.next()
        if frame.get("type") == "stream_event" and frame.get("parent_tool_use_id") == parent_call:
            return


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


@pytest.mark.parametrize("forward_text", [False, True], ids=["default", "forward"])
async def test_concurrent_children_forward_live_items_and_persist_separate_transcripts(
    claude: ClaudeLauncher, anthropic_messages: AnthropicMessages, forward_text: bool
) -> None:
    """Root completion is not child completion; sibling attribution survives reverse completion."""
    async with asyncio.timeout(90):
        async with claude.start(anthropic_messages, subagents=True, forward_subagent_text=forward_text) as run:
            prompt = await run.send("Delegate two independent probes.")
            async with AsyncExitStack() as exchanges:
                parent = await exchanges.enter_async_context(await anthropic_messages.await_next_request())
                children = {}
                for name in ("first", "second"):
                    assert "Agent" in parent.request.tool_names
                    await parent.send(
                        *anthropic_sse.message_stream(
                            [
                                anthropic_sse.ToolUse(
                                    f"spawn_{name}",
                                    "Agent",
                                    {
                                        "description": f"Probe {name}",
                                        "subagent_type": "general-purpose",
                                        "prompt": f"CHILD_{name}_PROMPT",
                                    },
                                )
                            ],
                            model=MODEL,
                        ).events
                    )
                    await parent.close()
                    first = await exchanges.enter_async_context(await anthropic_messages.await_next_request())
                    second = await exchanges.enter_async_context(await anthropic_messages.await_next_request())
                    parent, child = (first, second) if first.request.tool_results else (second, first)
                    assert parent.request.tool_results[-1].tool_use_id == f"spawn_{name}"
                    assert parent.request.tool_results[-1].is_error is False
                    assert f"CHILD_{name}_PROMPT" in "\n".join(child.request.texts("user"))
                    assert "Agent" in child.request.tool_names
                    children[name] = child
                await parent.send(
                    *anthropic_sse.message_stream([anthropic_sse.Text("PARENT_IDLE")], model=MODEL).events
                )
                await parent.close()
                root_result = await prompt.result()
                assert root_result.result == "PARENT_IDLE"
                root_id = root_result.session_id
                started = [frame for frame in run.native_frames() if frame.get("subtype") == "task_started"]
                assert {frame["tool_use_id"] for frame in started} == {"spawn_first", "spawn_second"}
                child_ids = {frame["tool_use_id"]: frame["task_id"] for frame in started}
                assert len(set(child_ids.values())) == 2
                assert all(frame["is_backgrounded"] for frame in started)
                assert not [frame for frame in run.native_frames() if frame.get("subtype") == "task_notification"]
                for name in ("second", "first"):
                    child = children[name]
                    observer = run.frames()
                    completion = run.events()
                    stream = anthropic_sse.message_stream(
                        [
                            anthropic_sse.Thinking(f"THINK_{name}", f"sig_{name}"),
                            anthropic_sse.Text(f"LIVE_{name}"),
                            anthropic_sse.ToolUse(
                                f"shell_{name}",
                                "Bash",
                                {"command": f"printf OUT_{name}; printf ERR_{name} >&2; exit 23"},
                            ),
                        ],
                        model=MODEL,
                    )
                    prefix = stream.through("text_delta")
                    await child.send(*prefix.events)

                    with pytest.raises(TimeoutError):
                        await asyncio.wait_for(wait_for_child_delta(observer, f"spawn_{name}"), timeout=0.25)
                    await child.send(*stream.events[len(prefix.events) :])
                    await child.close()
                    async with await anthropic_messages.await_next_request() as continued:
                        assert f"CHILD_{name}_PROMPT" in "\n".join(continued.request.texts("user"))
                        result = continued.request.tool_results[-1]
                        assert result.tool_use_id == f"shell_{name}"
                        assert result.is_error is True
                        assert f"OUT_{name}" in result.text
                        assert f"ERR_{name}" in result.text
                        while True:
                            frame = await observer.next()
                            if frame.get("type") == "user" and any(
                                block.get("tool_use_id") == f"shell_{name}" for block in frame["message"]["content"]
                            ):
                                assert frame["parent_tool_use_id"] == f"spawn_{name}"
                                break
                        assert not [
                            frame
                            for frame in run.native_frames()
                            if frame.get("subtype") == "task_notification"
                            and frame["task_id"] == child_ids[f"spawn_{name}"]
                        ]
                        await continued.send(
                            *anthropic_sse.message_stream([anthropic_sse.Text(f"DONE_{name}")], model=MODEL).events
                        )
                    async with await anthropic_messages.await_next_request() as notified:
                        assert f"DONE_{name}" in "\n".join(notified.request.texts("user"))
                        await notified.send(
                            *anthropic_sse.message_stream([anthropic_sse.Text(f"ACK_{name}")], model=MODEL).events
                        )
                    assert (await completion.result()).result == f"ACK_{name}"
                captured = run.native_frames()
                notifications = [frame for frame in captured if frame.get("subtype") == "task_notification"]
                assert [frame["task_id"] for frame in notifications] == [
                    child_ids["spawn_second"],
                    child_ids["spawn_first"],
                ]
                assert all(frame["status"] == "completed" for frame in notifications)
                for name in ("first", "second"):
                    child_frames = [frame for frame in captured if frame.get("parent_tool_use_id") == f"spawn_{name}"]
                    assert not [frame for frame in child_frames if frame.get("type") == "stream_event"]
                    texts = [
                        block["text"]
                        for frame in child_frames
                        if frame.get("type") == "assistant"
                        for block in frame["message"]["content"]
                        if block["type"] == "text"
                    ]
                    assert texts == [f"LIVE_{name}", f"DONE_{name}"]
                    thinking = [
                        block
                        for frame in child_frames
                        if frame.get("type") == "assistant"
                        for block in frame["message"]["content"]
                        if block["type"] == "thinking"
                    ]
                    assert thinking == [{"type": "thinking", "thinking": f"THINK_{name}", "signature": f"sig_{name}"}]
                    assert all(frame["session_id"] == root_id for frame in child_frames)
                    paths = list(claude.config.glob(f"projects/**/subagents/agent-{child_ids[f'spawn_{name}']}.jsonl"))
                    assert len(paths) == 1
                    while True:
                        transcript = paths[0].read_text()
                        if f"DONE_{name}" in transcript:
                            break
                        await asyncio.sleep(0.05)
                    for marker in (f"LIVE_{name}", f"DONE_{name}", f"OUT_{name}", f"ERR_{name}"):
                        assert marker in transcript
                    assert all(
                        frame["message"]["id"] in transcript
                        for frame in child_frames
                        if frame.get("type") == "assistant"
                    )
                    other = "second" if name == "first" else "first"
                    assert f"DONE_{other}" not in transcript
                (root_path,) = claude.config.glob(f"projects/*/{root_id}.jsonl")
                while True:
                    if "ACK_first" in root_path.read_text():
                        break
                    await asyncio.sleep(0.05)
                assert await run.crash() < 0
        async with claude.start(anthropic_messages, subagents=True, resume_id=root_id) as resumed:
            boundary = len(resumed.native_frames())
            recovery = await resumed.send("Report the completed probes without starting any new children.")
            async with await anthropic_messages.await_next_request() as exchange:
                history = "\n".join(exchange.request.texts("user"))
                assert "DONE_first" in history
                assert "DONE_second" in history
                await exchange.send(
                    *anthropic_sse.message_stream([anthropic_sse.Text("RECOVERED")], model=MODEL).events
                )
            assert (await recovery.result()).session_id == root_id
            replayed = resumed.native_frames()[boundary:]
            assert not [frame for frame in replayed if frame.get("subtype") in {"task_started", "task_notification"}]
            assert not [frame for frame in replayed if frame.get("parent_tool_use_id")]
            with pytest.raises(TimeoutError):
                async with asyncio.timeout(0.25):
                    await anthropic_messages.await_next_request()


@pytest.mark.parametrize("flag", ["--no-forward-subagent-text", "--forward-subagent-text=false"])
async def test_forwarding_has_no_cli_opt_out(claude: ClaudeLauncher, flag: str) -> None:
    async with AsyncNativeProcess(
        claude.logs, [claude.binary, flag], cwd=claude.workspace, environment=claude.base_environment
    ) as process:
        assert process.process is not None
        assert await asyncio.wait_for(process.process.wait(), timeout=10) == 1
    assert f"unknown option '{flag}'" in (claude.logs / "stderr.jsonl").read_text()
    assert process.stdout_frames() == []


@pytest.mark.parametrize("ending", ["crash-active", "model-error"])
async def test_child_failure_or_parent_crash_does_not_invent_child_completion(
    claude: ClaudeLauncher, anthropic_messages: AnthropicMessages, ending: str
) -> None:
    async with asyncio.timeout(90):
        async with claude.start(anthropic_messages, subagents=True) as run:
            prompt = await run.send("Delegate a probe that might fail.")
            async with await anthropic_messages.await_next_request() as exchange:
                assert "Agent" in exchange.request.tool_names
                await exchange.send(
                    *anthropic_sse.message_stream(
                        [
                            anthropic_sse.ToolUse(
                                "spawn_failure",
                                "Agent",
                                {
                                    "description": "Failure probe",
                                    "subagent_type": "general-purpose",
                                    "prompt": "CHILD_FAILURE_PROBE",
                                },
                            )
                        ],
                        model=MODEL,
                    ).events
                )
            async with (
                await anthropic_messages.await_next_request() as first,
                await anthropic_messages.await_next_request() as second,
            ):
                parent, child = (first, second) if first.request.tool_results else (second, first)
                assert parent.request.tool_results[-1].tool_use_id == "spawn_failure"
                assert "CHILD_FAILURE_PROBE" in "\n".join(child.request.texts("user"))
                await parent.send(
                    *anthropic_sse.message_stream([anthropic_sse.Text("PARENT_IDLE")], model=MODEL).events
                )
                await parent.close()
                result = await prompt.result()
                root_id = result.session_id
                (started,) = [frame for frame in run.native_frames() if frame.get("subtype") == "task_started"]
                child_id = started["task_id"]
                completion = run.events()
                if ending == "model-error":
                    await child.abort()
                    for _ in range(MAX_RETRIES):
                        async with await anthropic_messages.await_next_request() as retry:
                            assert "CHILD_FAILURE_PROBE" in "\n".join(retry.request.texts("user"))
                            await retry.abort()
                    async with await anthropic_messages.await_next_request() as notified:
                        assert child_id in "\n".join(notified.request.texts("user"))
                        await notified.send(
                            *anthropic_sse.message_stream([anthropic_sse.Text("PARENT_SURVIVED")], model=MODEL).events
                        )
                    assert (await completion.result()).is_error is False
                    (notification,) = [
                        frame for frame in run.native_frames() if frame.get("subtype") == "task_notification"
                    ]
                    assert notification["task_id"] == child_id
                    assert notification["tool_use_id"] == "spawn_failure"
                    assert notification["status"] == "failed"
                else:
                    assert not [frame for frame in run.native_frames() if frame.get("subtype") == "task_notification"]
                (root_path,) = claude.config.glob(f"projects/*/{root_id}.jsonl")
                marker = "PARENT_SURVIVED" if ending == "model-error" else "PARENT_IDLE"
                while True:
                    if marker in root_path.read_text():
                        break
                    await asyncio.sleep(0.05)
                assert await run.crash() < 0
                if ending == "crash-active":
                    await child.wait_client_closed()
        async with claude.start(anthropic_messages, subagents=True, resume_id=root_id) as resumed:
            boundary = len(resumed.native_frames())
            recovery = await resumed.send("Continue the parent without delegating again.")
            async with await anthropic_messages.await_next_request() as exchange:
                assert any(use.id == "spawn_failure" for use in exchange.request.tool_uses)
                await exchange.send(
                    *anthropic_sse.message_stream([anthropic_sse.Text("RECOVERED")], model=MODEL).events
                )
            assert (await recovery.result()).session_id == root_id
            assert not [
                frame
                for frame in resumed.native_frames()[boundary:]
                if frame.get("subtype") in {"task_started", "task_notification"}
            ]
            with pytest.raises(TimeoutError):
                async with asyncio.timeout(0.25):
                    await anthropic_messages.await_next_request()


async def test_nested_child_tool_frames_identify_the_immediate_parent_call(
    claude: ClaudeLauncher, anthropic_messages: AnthropicMessages
) -> None:
    async with asyncio.timeout(60):
        async with claude.start(anthropic_messages, subagents=True) as run:
            await run.send("Delegate a nested probe.")
            async with AsyncExitStack() as exchanges:
                current = await exchanges.enter_async_context(await anthropic_messages.await_next_request())
                parents = []
                for name in ("child", "grandchild"):
                    assert "Agent" in current.request.tool_names
                    await current.send(
                        *anthropic_sse.message_stream(
                            [
                                anthropic_sse.ToolUse(
                                    f"spawn_{name}",
                                    "Agent",
                                    {
                                        "description": f"Nested {name}",
                                        "subagent_type": "general-purpose",
                                        "prompt": f"NESTED_{name}",
                                    },
                                )
                            ],
                            model=MODEL,
                        ).events
                    )
                    await current.close()
                    first = await exchanges.enter_async_context(await anthropic_messages.await_next_request())
                    second = await exchanges.enter_async_context(await anthropic_messages.await_next_request())
                    parent, current = (first, second) if first.request.tool_results else (second, first)
                    assert parent.request.tool_results[-1].tool_use_id == f"spawn_{name}"
                    assert f"NESTED_{name}" in "\n".join(current.request.texts("user"))
                    parents.append(parent)
                observer = run.frames()
                await current.send(
                    *anthropic_sse.message_stream(
                        [anthropic_sse.ToolUse("grandchild_shell", "Bash", {"command": "printf GRANDCHILD_OUTPUT"})],
                        model=MODEL,
                    ).events
                )
                await current.close()
                async with await anthropic_messages.await_next_request() as grandchild:
                    assert grandchild.request.tool_results[-1].tool_use_id == "grandchild_shell"
                    assert "GRANDCHILD_OUTPUT" in grandchild.request.tool_results[-1].text
                    while True:
                        frame = await observer.next()
                        if frame.get("type") == "assistant" and any(
                            block.get("id") == "grandchild_shell" for block in frame["message"]["content"]
                        ):
                            assert frame["parent_tool_use_id"] == "spawn_grandchild"
                            break
                    captured = run.native_frames()
                    nested_spawn = [
                        frame
                        for frame in captured
                        if frame.get("type") == "assistant"
                        and any(block.get("id") == "spawn_grandchild" for block in frame["message"]["content"])
                    ]
                    assert len(nested_spawn) == 1
                    assert nested_spawn[0]["parent_tool_use_id"] == "spawn_child"
                    started = [frame for frame in captured if frame.get("subtype") == "task_started"]
                    assert {frame["tool_use_id"] for frame in started} == {"spawn_child", "spawn_grandchild"}
                    assert len({frame["task_id"] for frame in started}) == 2
                    assert not [frame for frame in captured if frame.get("subtype") == "task_notification"]
                    assert await run.crash() < 0
                    await grandchild.wait_client_closed()
                for parent in parents:
                    await parent.wait_client_closed()
