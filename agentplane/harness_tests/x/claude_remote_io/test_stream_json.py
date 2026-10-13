"""Same-version control: child discovery and native-file resume without RemoteIO."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest
import pytest_bazel

from agentplane.harness_tests.claude import anthropic_sse
from agentplane.harness_tests.claude.harness import ClaudeLauncher
from agentplane.harness_tests.claude.messages import AnthropicMessages
from agentplane.harness_tests.claude.model import MODEL
from agentplane.native.claude import wire
from util.bazel.runfiles import get_required_path
from util.testing.undeclared_outputs import undeclared_outputs_dir


@pytest.mark.parametrize("scenario", ["clean-completed", "crash-completed", "crash-active"])
async def test_child_resume(tmp_path: Path, scenario: str) -> None:
    crash = scenario != "clean-completed"
    active = scenario == "crash-active"
    logs = undeclared_outputs_dir() / f"stdio-{scenario}"
    logs.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    config = home / ".claude"
    config.mkdir()
    harness = ClaudeLauncher(
        workspace=tmp_path,
        logs=logs,
        config=config,
        binary=str(get_required_path("claude_remote_io_cli_linux_x64/claude")),
        base_environment={"HOME": str(home), "PATH": os.environ.get("PATH", "/usr/bin:/bin"), "NO_PROXY": "127.0.0.1"},
    )
    async with AnthropicMessages() as model:
        async with harness.start(model, subagents=True) as run:
            async with asyncio.timeout(45):
                prompt = await run.send("Delegate a child probe.")
                async with await model.await_next_request() as exchange:
                    assert exchange.request.texts("user")[-1] == "Delegate a child probe."
                    assert "Agent" in exchange.request.tool_names
                    assert "TaskOutput" not in exchange.request.tool_names
                    await exchange.send(
                        *anthropic_sse.message_stream(
                            [
                                anthropic_sse.ToolUse(
                                    "toolu_stdio_child",
                                    "Agent",
                                    {
                                        "description": "Same-version child probe",
                                        "subagent_type": "general-purpose",
                                        "prompt": "Reply STDIO_CHILD_DONE.",
                                    },
                                )
                            ],
                            model=MODEL,
                        ).events
                    )
                async with await model.await_next_request() as first, await model.await_next_request() as second:
                    parent, child = (first, second) if first.request.tool_results else (second, first)
                    (launch,) = parent.request.tool_results
                    assert launch.tool_use_id == "toolu_stdio_child"
                    assert launch.is_error is False
                    assert "Reply STDIO_CHILD_DONE." in "\n".join(child.request.texts("user"))
                    await parent.send(
                        *anthropic_sse.message_stream([anthropic_sse.Text("STDIO_PARENT_WAITING")], model=MODEL).events
                    )
                    await parent.close()
                    initial = await prompt.result()
                    assert initial.result == "STDIO_PARENT_WAITING"
                    completed = run.events()
                    if active:
                        assert await run.crash() < 0
                        await child.wait_client_closed()
                    else:
                        await child.send(
                            *anthropic_sse.message_stream([anthropic_sse.Text("STDIO_CHILD_DONE")], model=MODEL).events
                        )
                if not active:
                    async with await model.await_next_request() as exchange:
                        assert "STDIO_CHILD_DONE" in "\n".join(exchange.request.texts("user"))
                        await exchange.send(
                            *anthropic_sse.message_stream([anthropic_sse.Text("STDIO_PARENT_DONE")], model=MODEL).events
                        )
                    assert (await completed.result()).result == "STDIO_PARENT_DONE"
                    if crash:
                        assert await run.crash() < 0
        captured = run.native_frames()
        (started,) = [frame for frame in captured if frame.get("subtype") == "task_started"]
        if active:
            assert not any(frame.get("subtype") == "task_notification" for frame in captured)
        else:
            (finished,) = [frame for frame in captured if frame.get("subtype") == "task_notification"]
            assert started["task_id"] == finished["task_id"]
            assert started["tool_use_id"] == finished["tool_use_id"] == "toolu_stdio_child"
            assert finished["status"] == "completed"
            assert finished["session_id"] == initial.session_id
            assert any(
                frame.get("type") == "assistant"
                and frame.get("parent_tool_use_id") == "toolu_stdio_child"
                and frame["session_id"] == initial.session_id
                and {"type": "text", "text": "STDIO_CHILD_DONE"} in frame["message"]["content"]
                for frame in captured
            )
        async with harness.start(model, subagents=True, resume_id=initial.session_id, initialize=False) as resumed:
            async with asyncio.timeout(45):
                # Keep background results independently observable while the
                # shared facade waits for the correlated initialization reply.
                automatic = resumed.events()
                reply = await resumed.initialize()
                assert isinstance(reply, wire.ControlResponseFrame)
                assert reply.response.subtype == "success"
                if active:
                    notification_result = await automatic.result()
                    assert not notification_result.is_error
                    assert notification_result.result == ""
                    assert notification_result.num_turns == 0
                    events = resumed.events()
                    while not any(
                        frame.get("subtype") == "task_notification"
                        for frame in resumed.native_frames()[len(captured) :]
                    ):
                        await events.next()
                    (stopped,) = [
                        frame
                        for frame in resumed.native_frames()[len(captured) :]
                        if frame.get("subtype") == "task_notification"
                    ]
                    assert stopped["task_id"] == started["task_id"]
                    assert stopped["status"] == "stopped"
                    assert "didn't finish before the previous session ended" in stopped["summary"]
                recovery = await resumed.send("Read the old child's result without restarting it.")
                async with await model.await_next_request() as exchange:
                    if active:
                        assert "STDIO_PARENT_WAITING" in exchange.request.texts("assistant")
                    else:
                        assert "STDIO_CHILD_DONE" in "\n".join(exchange.request.texts("user"))
                    assert "TaskOutput" not in exchange.request.tool_names
                    await exchange.send(
                        *anthropic_sse.message_stream(
                            [
                                anthropic_sse.ToolUse(
                                    "toolu_stdio_read", "TaskOutput", {"task_id": started["task_id"], "block": False}
                                )
                            ],
                            model=MODEL,
                        ).events
                    )
                async with await model.await_next_request() as exchange:
                    (read,) = exchange.request.tool_results
                    assert read.is_error is True
                    assert "No such tool available: TaskOutput" in read.text
                    await exchange.send(
                        *anthropic_sse.message_stream([anthropic_sse.Text("STDIO_RESUMED")], model=MODEL).events
                    )
                assert (await recovery.result()).result == "STDIO_RESUMED"
                notifications = [
                    frame
                    for frame in resumed.native_frames()[len(captured) :]
                    if frame.get("subtype") == "task_notification"
                ]
                assert len(notifications) == (1 if active else 0)


if __name__ == "__main__":
    pytest_bazel.main()
