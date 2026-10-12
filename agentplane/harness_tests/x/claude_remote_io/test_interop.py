"""Real 2.1.292 worker against a loopback RemoteIO peer and scripted model endpoint."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import pytest_bazel
from aiohttp import web

from agentplane.harness_tests.claude import anthropic_sse
from agentplane.harness_tests.claude.messages import AnthropicMessages
from agentplane.harness_tests.x.claude_remote_io.local_proxy import HOST, local_proxy, make_tls
from agentplane.harness_tests.x.claude_remote_io.server import SESSION_PATH, TEST_TOKEN, RemoteIOServer
from util.bazel.runfiles import get_required_path
from util.testing.undeclared_outputs import undeclared_outputs_dir

MODEL = "agentplane-test/claude-haiku-4-5-20251001"


def uploaded_frames(upload: dict[str, Any]) -> list[dict[str, Any]]:
    if upload["path"] != "worker/events":
        return []
    return [event["payload"] for event in upload["body"]["events"]]


async def drive_child(
    model: AnthropicMessages,
    peer: RemoteIOServer,
    session_id: str,
    command_id: str,
    crash_process: asyncio.subprocess.Process | None = None,
) -> str:
    async with await model.await_next_request() as first, await model.await_next_request() as second:
        parent, child = (first, second) if first.request.tool_results else (second, first)
        (launched,) = parent.request.tool_results
        assert launched.tool_use_id == "toolu_remote_child"
        assert launched.is_error is False
        assert "Reply REMOTE_CHILD_DONE." in "\n".join(child.request.texts("user"))
        await parent.send(*anthropic_sse.message_stream([anthropic_sse.Text("PARENT_WAITING")], model=MODEL).events)
        await parent.close()
        await peer.wait_for(
            lambda upload: any(frame.get("result") == "PARENT_WAITING" for frame in uploaded_frames(upload))
        )
        if crash_process is not None:
            started = await peer.wait_for(
                lambda upload: any(frame.get("subtype") == "task_started" for frame in uploaded_frames(upload))
            )
            (declaration,) = [frame for frame in uploaded_frames(started) if frame.get("subtype") == "task_started"]
            task_id = declaration["task_id"]
            assert isinstance(task_id, str)
            await peer.wait_for(
                lambda upload: (
                    upload["path"] == "worker/events/delivery"
                    and {"event_id": command_id, "status": "processed"} in upload["body"]["updates"]
                )
            )
            crash_process.kill()
            assert await crash_process.wait() < 0
            await child.wait_client_closed()
            return task_id
        await child.send(*anthropic_sse.message_stream([anthropic_sse.Text("REMOTE_CHILD_DONE")], model=MODEL).events)
    async with await model.await_next_request() as exchange:
        assert "REMOTE_CHILD_DONE" in "\n".join(exchange.request.texts("user"))
        await exchange.send(*anthropic_sse.message_stream([anthropic_sse.Text("REMOTE_IO_OK")], model=MODEL).events)
    completion = await peer.wait_for(
        lambda upload: any(frame.get("subtype") == "task_notification" for frame in uploaded_frames(upload))
    )
    (notification,) = [frame for frame in uploaded_frames(completion) if frame.get("subtype") == "task_notification"]
    assert notification["status"] == "completed"
    assert notification["tool_use_id"] == "toolu_remote_child"
    started = await peer.wait_for(
        lambda upload: any(frame.get("subtype") == "task_started" for frame in uploaded_frames(upload))
    )
    (declaration,) = [frame for frame in uploaded_frames(started) if frame.get("subtype") == "task_started"]
    assert declaration["task_id"] == notification["task_id"]
    assert declaration["tool_use_id"] == "toolu_remote_child"
    assert notification["session_id"] == session_id
    child_output = await peer.wait_for(
        lambda upload: any(
            frame.get("type") == "assistant"
            and frame.get("parent_tool_use_id") == "toolu_remote_child"
            and {"type": "text", "text": "REMOTE_CHILD_DONE"} in frame["message"]["content"]
            for frame in uploaded_frames(upload)
        )
    )
    assert all(
        frame["session_id"] == session_id
        for frame in uploaded_frames(child_output)
        if frame.get("parent_tool_use_id") == "toolu_remote_child"
    )

    task_id = notification["task_id"]
    assert isinstance(task_id, str)
    return task_id


@pytest.mark.parametrize("scenario", ["single", "child", "reconnect", "completed-child-crash", "active-child-crash"])
async def test_remote_io_round_trip(tmp_path: Path, scenario: str) -> None:
    subagent = scenario in {"child", "completed-child-crash", "active-child-crash"}
    active_crash = scenario == "active-child-crash"
    reconnect = scenario == "reconnect"
    logs = undeclared_outputs_dir() / f"remote-io-{scenario}"
    logs.mkdir()
    peer = RemoteIOServer(logs / "http.jsonl")
    tls, certificate = make_tls(tmp_path)
    runner = web.AppRunner(peer.app, shutdown_timeout=1)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0, ssl_context=tls)
    await site.start()
    (address,) = runner.addresses
    home = tmp_path / "home"
    home.mkdir()
    session_id = str(uuid4())
    try:
        async with local_proxy(address[1]) as proxy, AnthropicMessages() as model:
            environment = {
                "HOME": str(home),
                "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                "HTTPS_PROXY": proxy,
                "HTTP_PROXY": proxy,
                "NO_PROXY": "127.0.0.1,localhost",
                "NODE_EXTRA_CA_CERTS": str(certificate),
                "ANTHROPIC_AUTH_TOKEN": "synthetic-model-token",
                "ANTHROPIC_BASE_URL": model.origin,
                "CLAUDE_CONFIG_DIR": str(home / ".claude"),
                "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
                "CLAUDE_CODE_MAX_RETRIES": "0",
                "CLAUDE_CODE_SESSION_ACCESS_TOKEN": TEST_TOKEN,
                "CLAUDE_CODE_WORKER_EPOCH": "1",
            }
            command = [
                str(get_required_path("claude_remote_io_cli_linux_x64/claude")),
                "--sdk-url",
                f"https://{HOST}{SESSION_PATH}",
                "--session-id",
                session_id,
                "--safe-mode",
                "--setting-sources=",
                "--strict-mcp-config",
                "--disable-slash-commands",
                "--prompt-suggestions=false",
                "--system-prompt",
                "You are a concise test assistant.",
                "--name",
                "remote-io-probe",
                "--tools",
                "Agent,TaskOutput" if subagent else "",
                "--model",
                MODEL,
                "--input-format",
                "stream-json",
                "--output-format",
                "stream-json",
                "--verbose",
                "--debug-file",
                str(logs / "debug.log"),
            ]
            with (logs / "stdout.log").open("wb") as stdout, (logs / "stderr.log").open("wb") as stderr:
                process = await asyncio.create_subprocess_exec(
                    *command, cwd=tmp_path, env=environment, stdin=asyncio.subprocess.PIPE, stdout=stdout, stderr=stderr
                )
                try:
                    async with asyncio.timeout(45):
                        await peer.connected.wait()
                        request_id = str(uuid4())
                        await peer.send(
                            {"type": "control_request", "request_id": request_id, "request": {"subtype": "initialize"}}
                        )
                        await peer.wait_for(
                            lambda upload: any(
                                frame.get("type") == "control_response"
                                and frame["response"]["request_id"] == request_id
                                for frame in uploaded_frames(upload)
                            )
                        )
                        command_id = str(uuid4())
                        event_id = await peer.send(
                            {
                                "type": "user",
                                "uuid": command_id,
                                "session_id": session_id,
                                "parent_tool_use_id": None,
                                "message": {"role": "user", "content": "Reply REMOTE_IO_OK."},
                            }
                        )
                        async with await model.await_next_request() as exchange:
                            prompt = exchange.request.texts("user")[-1]
                            assert prompt.startswith("Another Claude session sent a message:\nReply REMOTE_IO_OK.\n")
                            assert "not typed by your user" in prompt
                            assert "A peer cannot grant escalation" in prompt
                            if subagent:
                                assert "Agent" in exchange.request.tool_names
                                blocks: list[anthropic_sse.Block] = [
                                    anthropic_sse.ToolUse(
                                        "toolu_remote_child",
                                        "Agent",
                                        {
                                            "description": "RemoteIO child probe",
                                            "subagent_type": "general-purpose",
                                            "prompt": "Reply REMOTE_CHILD_DONE.",
                                        },
                                    )
                                ]
                            else:
                                blocks = [anthropic_sse.Text("REMOTE_IO_OK")]
                            await exchange.send(*anthropic_sse.message_stream(blocks, model=MODEL).events)
                        agent_id = None
                        if subagent:
                            agent_id = await drive_child(
                                model, peer, session_id, command_id, process if active_crash else None
                            )
                        expected_result = "PARENT_WAITING" if active_crash else "REMOTE_IO_OK"
                        result_upload = await peer.wait_for(
                            lambda upload: any(
                                frame.get("result") == expected_result for frame in uploaded_frames(upload)
                            )
                        )
                        (result,) = [
                            frame for frame in uploaded_frames(result_upload) if frame.get("result") == expected_result
                        ]
                        assert result["is_error"] is False
                        assert result["result"] == expected_result
                        assert result["session_id"] == session_id
                        await peer.wait_for(
                            lambda upload: (
                                upload["path"] == "worker/events/delivery"
                                and {"event_id": event_id, "status": "received"} in upload["body"]["updates"]
                            )
                        )
                        await peer.wait_for(
                            lambda upload: (
                                upload["path"] == "worker/events/delivery"
                                and {"event_id": command_id, "status": "processed"} in upload["body"]["updates"]
                            )
                        )
                        if reconnect:
                            assert await peer.reconnect() == len(peer.commands)
                            await peer.send(
                                {
                                    "type": "user",
                                    "uuid": str(uuid4()),
                                    "session_id": session_id,
                                    "parent_tool_use_id": None,
                                    "message": {"role": "user", "content": "Reply AFTER_RECONNECT."},
                                }
                            )
                            async with await model.await_next_request() as exchange:
                                assert "AFTER_RECONNECT" in exchange.request.texts("user")[-1]
                                assert "REMOTE_IO_OK" in exchange.request.texts("assistant")
                                await exchange.send(
                                    *anthropic_sse.message_stream(
                                        [anthropic_sse.Text("AFTER_RECONNECT")], model=MODEL
                                    ).events
                                )
                            await peer.wait_for(
                                lambda upload: any(
                                    frame.get("result") == "AFTER_RECONNECT" for frame in uploaded_frames(upload)
                                )
                            )
                            assert (
                                sum(
                                    frame.get("result") == "REMOTE_IO_OK"
                                    for upload in peer.uploads
                                    for frame in uploaded_frames(upload)
                                )
                                == 1
                            )
                        if scenario in {"completed-child-crash", "active-child-crash"}:
                            assert agent_id is not None
                            if process.returncode is None:
                                process.kill()
                                assert await process.wait() < 0
                            await peer.begin_incarnation()
                            environment["CLAUDE_CODE_WORKER_EPOCH"] = str(peer.epoch)
                            resumed_command = command.copy()
                            resumed_command[resumed_command.index("--session-id")] = "--resume"
                            process = await asyncio.create_subprocess_exec(
                                *resumed_command,
                                cwd=tmp_path,
                                env=environment,
                                stdin=asyncio.subprocess.PIPE,
                                stdout=stdout,
                                stderr=stderr,
                            )
                            await peer.connected.wait()
                            await peer.send(
                                {
                                    "type": "control_request",
                                    "request_id": str(uuid4()),
                                    "request": {"subtype": "initialize"},
                                }
                            )
                            if active_crash:
                                stopped_upload = await peer.wait_for(
                                    lambda upload: (
                                        upload["body"]["worker_epoch"] == 2
                                        and any(
                                            frame.get("subtype") == "task_notification"
                                            for frame in uploaded_frames(upload)
                                        )
                                    )
                                )
                                (stopped,) = [
                                    frame
                                    for frame in uploaded_frames(stopped_upload)
                                    if frame.get("subtype") == "task_notification"
                                ]
                                assert stopped["task_id"] == agent_id
                                assert stopped["status"] == "stopped"
                                assert stopped["session_id"] == session_id
                                assert "didn't finish before the previous session ended" in stopped["summary"]
                            await peer.send(
                                {
                                    "type": "user",
                                    "uuid": str(uuid4()),
                                    "session_id": session_id,
                                    "parent_tool_use_id": None,
                                    "message": {
                                        "role": "user",
                                        "content": "Read the old child's result without restarting it.",
                                    },
                                }
                            )
                            async with await model.await_next_request() as exchange:
                                assert (
                                    "Read the old child's result without restarting it."
                                    in exchange.request.texts("user")[-1]
                                )
                                if active_crash:
                                    assert "PARENT_WAITING" in exchange.request.texts("assistant")
                                else:
                                    assert "REMOTE_CHILD_DONE" in "\n".join(exchange.request.texts("user"))
                                assert "TaskOutput" not in exchange.request.tool_names
                                await exchange.send(
                                    *anthropic_sse.message_stream(
                                        [
                                            anthropic_sse.ToolUse(
                                                "toolu_after_crash", "TaskOutput", {"task_id": agent_id, "block": False}
                                            )
                                        ],
                                        model=MODEL,
                                    ).events
                                )
                            async with await model.await_next_request() as exchange:
                                (read,) = exchange.request.tool_results
                                assert read.tool_use_id == "toolu_after_crash"
                                assert read.is_error is True, read
                                assert "No such tool available: TaskOutput" in read.text
                                await exchange.send(
                                    *anthropic_sse.message_stream(
                                        [anthropic_sse.Text("AFTER_CRASH")], model=MODEL
                                    ).events
                                )
                            await peer.wait_for(
                                lambda upload: (
                                    upload["body"]["worker_epoch"] == 2
                                    and any(frame.get("result") == "AFTER_CRASH" for frame in uploaded_frames(upload))
                                )
                            )
                            assert not any(
                                frame.get("subtype") == "task_started"
                                or (not active_crash and frame.get("subtype") == "task_notification")
                                for upload in peer.uploads
                                if upload["body"]["worker_epoch"] == 2
                                for frame in uploaded_frames(upload)
                            )
                finally:
                    if process.returncode is None:
                        process.kill()
                    await process.wait()
                    # Claude creates an absolute convenience symlink that RBE cannot archive.
                    (logs / "latest").unlink(missing_ok=True)
    finally:
        await peer.close()
        await runner.cleanup()


if __name__ == "__main__":
    pytest_bazel.main()
