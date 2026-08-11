"""Compatibility tests for the executable Claude CLI bridge."""

from __future__ import annotations

import contextlib
from functools import partial
from pathlib import Path

import anyio
import pytest
import pytest_bazel
from claude_agent_sdk import ClaudeAgentOptions
from claude_agent_sdk._internal.transport.subprocess_cli import SubprocessCLITransport

from haku.runtime.x.agent_sdk_transport.options import build_claude_launch, enable_fine_grained_streaming
from haku.runtime.x.agent_sdk_transport.protocol import (
    FINE_GRAINED_TOOL_STREAMING_ENV,
    RUNNER_TO_CONSOLE,
    ClaudeLaunch,
    ClaudeMessage,
    EndInput,
    Progress,
)
from haku.runtime.x.agent_sdk_transport.runner import (
    bridge_websocket_to_claude,
    build_claude_command,
    build_claude_environment,
    prepare_workspace,
)


class MemoryWebSocket:
    def __init__(self, *, incoming: anyio.abc.ObjectReceiveStream[str], outgoing: anyio.abc.ObjectSendStream[str]):
        self._incoming = incoming
        self._outgoing = outgoing
        self.closed = False

    async def send_text(self, data: str) -> None:
        await self._outgoing.send(data)

    async def receive_text(self) -> str:
        try:
            return await self._incoming.receive()
        except anyio.EndOfStream as error:
            raise EOFError from error

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        await self._outgoing.aclose()
        await self._incoming.aclose()


def memory_websocket_pair() -> tuple[MemoryWebSocket, MemoryWebSocket]:
    left_to_right_send, left_to_right_receive = anyio.create_memory_object_stream[str](16)
    right_to_left_send, right_to_left_receive = anyio.create_memory_object_stream[str](16)
    return (
        MemoryWebSocket(incoming=right_to_left_receive, outgoing=left_to_right_send),
        MemoryWebSocket(incoming=left_to_right_receive, outgoing=right_to_left_send),
    )


def test_launch_matches_the_pinned_sdk_and_uses_its_bundled_claude(tmp_path: Path) -> None:
    options = enable_fine_grained_streaming(
        ClaudeAgentOptions(
            cwd=tmp_path,
            permission_mode="dontAsk",
            setting_sources=[],
            system_prompt="remote prompt",
            tools=[],
            mcp_servers={
                "haku-console": {
                    "type": "http",
                    "url": "http://haku-console.test/mcp",
                    "headers": {"Authorization": "Bearer test-static-agent-token"},
                }
            },
        )
    )
    launch = build_claude_launch(options)
    sdk_transport = SubprocessCLITransport(prompt="", options=options)
    bundled_cli = Path(sdk_transport._find_cli())
    sdk_transport._cli_path = str(bundled_cli)

    assert bundled_cli.is_file()
    assert bundled_cli.name == "claude"
    assert build_claude_command(bundled_cli, launch) == sdk_transport._build_command()
    assert launch.cwd == str(tmp_path)
    assert launch.environment[FINE_GRAINED_TOOL_STREAMING_ENV] == "1"
    mcp_config = launch.arguments[launch.arguments.index("--mcp-config") + 1]
    assert "http://haku-console.test/mcp" in mcp_config
    assert "Bearer test-static-agent-token" in mcp_config


def test_environment_does_not_expose_the_bridge_credential(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAUDECODE", "parent")
    monkeypatch.setenv("HAKU_AGENT_SDK_RUNNER_TOKEN", "bridge-secret")
    launch = ClaudeLaunch(
        arguments=(),
        cwd="/workspace",
        environment={
            "CLAUDECODE": "injected-parent",
            "HAKU_AGENT_SDK_RUNNER_TOKEN": "injected-secret",
            "SAFE": "value",
        },
    )

    environment = build_claude_environment(launch)

    assert environment["CLAUDECODE"] == "injected-parent"
    assert "HAKU_AGENT_SDK_RUNNER_TOKEN" not in environment
    assert environment["SAFE"] == "value"


async def test_bridge_copies_json_between_websocket_and_cli_stdio(tmp_path: Path) -> None:
    fake_claude = tmp_path / "claude"
    fake_claude.write_text(
        "#!/usr/bin/env python3\nimport sys\nfor line in sys.stdin:\n    print(line, end='', flush=True)\n"
    )
    fake_claude.chmod(0o755)
    launch = ClaudeLaunch(arguments=(), cwd=str(tmp_path), environment={})
    console_socket, runner_socket = memory_websocket_pair()

    async with anyio.create_task_group() as tasks:
        tasks.start_soon(partial(bridge_websocket_to_claude, runner_socket, claude_path=fake_claude, launch=launch))
        message = {"type": "user", "message": {"role": "user", "content": "hello"}}
        await console_socket.send_text(ClaudeMessage(payload=message).model_dump_json())
        with anyio.fail_after(5):
            # Unwrapped on the way to the CLI and re-wrapped on the way back, so the echo
            # proves the runner strips and restores the envelope rather than passing it through.
            assert RUNNER_TO_CONSOLE.validate_json(await console_socket.receive_text()) == ClaudeMessage(
                payload=message
            )
        await console_socket.send_text(EndInput().model_dump_json())

    assert runner_socket.closed


def executable(path: Path, body: str) -> Path:
    path.write_text(f"#!/usr/bin/env bash\n{body}\n")
    path.chmod(0o755)
    return path


async def test_workspace_setup_runs_in_the_launch_directory(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    setup = executable(tmp_path / "setup.sh", "pwd > marker")

    await prepare_workspace(setup, cwd=str(workspace))

    assert (workspace / "marker").read_text().strip() == str(workspace)


async def test_workspace_setup_streams_its_output(tmp_path: Path) -> None:
    """Every line, verbatim — including whatever the tools it drives print, and stderr."""
    console_socket, runner_socket = memory_websocket_pair()
    setup = executable(
        tmp_path / "setup.sh",
        "echo \"Cloning into 'haku-state'...\"\necho\necho 'trouble' >&2\nprintf 'no trailing newline'",
    )

    await prepare_workspace(setup, cwd=str(tmp_path), websocket=runner_socket)
    await runner_socket.close()

    reported = []
    with contextlib.suppress(EOFError):
        while True:
            reported.append(RUNNER_TO_CONSOLE.validate_json(await console_socket.receive_text()))
    # The blank line is dropped; the unterminated last line is not.
    assert reported == [
        Progress(line="Cloning into 'haku-state'..."),
        Progress(line="trouble"),
        Progress(line="no trailing newline"),
    ]


async def test_workspace_setup_failure_is_fatal(tmp_path: Path) -> None:
    """No checkout means no manual, and a Claude that starts anyway is a generic assistant."""
    setup = executable(tmp_path / "setup.sh", "echo 'no credential' >&2; exit 3")

    with pytest.raises(RuntimeError, match="exited with status 3"):
        await prepare_workspace(setup, cwd=str(tmp_path))


if __name__ == "__main__":
    pytest_bazel.main()
