"""Compatibility tests for the executable Claude CLI bridge."""

from __future__ import annotations

import contextlib
from collections import deque
from functools import partial
from http import HTTPStatus
from pathlib import Path

import anyio
import pytest
import pytest_bazel
from websockets.asyncio.server import ServerConnection, serve
from websockets.http11 import Request, Response

from haku.runtime.x.claude_bridge.options import ClaudeSession, HttpMcpServer, build_claude_launch
from haku.runtime.x.claude_bridge.protocol import (
    FINE_GRAINED_TOOL_STREAMING_ENV,
    RUNNER_TO_CONSOLE,
    ClaudeLaunch,
    ClaudeMessage,
    EndInput,
    SetupOutput,
)
from haku.runtime.x.claude_bridge.runner import (
    REPLAY_WINDOW,
    Outbound,
    _serve_console,
    _shutdown,
    _start_claude,
    bridge_websocket_to_claude,
    build_claude_command,
    build_claude_environment,
    prepare_workspace,
    run,
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


def test_the_runner_runs_the_launch_the_console_sent(tmp_path: Path) -> None:
    """The sandbox side of the launch: the CLI path is the runner's to choose, everything after
    it is the console's.

    This used to assert our argv equalled the SDK's `_build_command()`, and to locate the CLI
    through the SDK's own `_find_cli()`. Both are gone with the dependency; the argv itself is
    pinned in `test_options.py`, and that the image really carries an extracted `claude` is a
    property of the `claude_executable` genrule the image build exercises.
    """
    launch = build_claude_launch(
        ClaudeSession(
            cwd=tmp_path,
            permission_mode="dontAsk",
            append_system_prompt="remote prompt",
            mcp_servers={
                "haku-console": HttpMcpServer(
                    url="http://haku-console.test/mcp", headers={"Authorization": "Bearer test-static-agent-token"}
                )
            },
        )
    )

    assert build_claude_command(Path("/usr/local/bin/claude"), launch) == ["/usr/local/bin/claude", *launch.arguments]
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


async def test_what_the_cli_writes_to_stderr_reaches_the_console(tmp_path: Path) -> None:
    """The one place a CLI that fails to start explains itself.

    It went to `DEVNULL`, so a rejected credential or a bad flag reached the console as
    `Claude Code exited with status 1` and nothing else.
    """
    fake_claude = tmp_path / "claude"
    fake_claude.write_text(
        "#!/usr/bin/env python3\nimport sys\nprint('cannot start: no credential', file=sys.stderr, flush=True)\n"
    )
    fake_claude.chmod(0o755)
    launch = ClaudeLaunch(arguments=(), cwd=str(tmp_path), environment={})
    console_socket, runner_socket = memory_websocket_pair()

    async with anyio.create_task_group() as tasks:
        tasks.start_soon(partial(bridge_websocket_to_claude, runner_socket, claude_path=fake_claude, launch=launch))
        with anyio.fail_after(5):
            assert RUNNER_TO_CONSOLE.validate_json(await console_socket.receive_text()) == SetupOutput(
                data=b"cannot start: no credential\n"
            )


async def test_the_cli_keeps_running_when_a_console_connection_ends(tmp_path: Path) -> None:
    """The property the whole roll-survival design rests on.

    `bridge_websocket_to_claude` used to terminate Claude in its `finally`, so one dropped socket
    ended the conversation; `_serve_console` returning is now just this connection ending.
    """
    fake_claude = tmp_path / "claude"
    fake_claude.write_text("#!/usr/bin/env python3\nimport time\ntime.sleep(30)\n")
    fake_claude.chmod(0o755)
    launch = ClaudeLaunch(arguments=(), cwd=str(tmp_path), environment={})
    console_socket, runner_socket = memory_websocket_pair()
    outbound_sender, outbound_receiver = anyio.create_memory_object_stream[Outbound](8)

    process = await _start_claude(fake_claude, launch)
    try:
        await console_socket.close()
        with anyio.fail_after(5):
            await _serve_console(runner_socket, process, outbound_receiver)
        assert process.returncode is None, "the CLI must outlive the connection that was serving it"
    finally:
        outbound_sender.close()
        await _shutdown(process)


async def test_the_runner_waits_out_a_missing_console_but_not_a_refusing_one(tmp_path: Path) -> None:
    """The refusal a crashloop is made of, and the outage that is not one.

    All three of the console's refusal paths close before `accept()`, and an ASGI server answers
    such a handshake with `403`, never a close code — so the close-code check this replaced could
    not fire. Worse, `InvalidStatus` is not an `OSError`: a single `503` from a Gateway with no
    ready backend, which is exactly what a console roll looks like from in here, escaped `run()`
    and took the sandbox with it.
    """
    answered: list[int] = []

    def answer(connection: ServerConnection, request: Request) -> Response:
        status = HTTPStatus.SERVICE_UNAVAILABLE if not answered else HTTPStatus.FORBIDDEN
        answered.append(status)
        return connection.respond(status, "")

    async def never_reached(connection: ServerConnection) -> None:
        raise AssertionError("a rejected handshake must not reach the handler")

    async with serve(never_reached, host="127.0.0.1", port=0, process_request=answer) as server:
        port = next(iter(server.sockets)).getsockname()[1]
        with anyio.fail_after(30):
            # Returns rather than raising: the sandbox is done, and a runner that exits nonzero
            # here is one Kubernetes restarts into the same refusal.
            await run(f"ws://127.0.0.1:{port}", tmp_path / "claude", None)

    assert answered == [HTTPStatus.SERVICE_UNAVAILABLE, HTTPStatus.FORBIDDEN]


async def test_a_second_console_is_handed_what_the_first_may_not_have_recorded(tmp_path: Path) -> None:
    """The point of the window. A frame handed to a dying socket may or may not have been
    recorded, and nothing at this end can tell the two apart — so it is offered again, and the
    console drops what it already has by the agent's own id."""
    process = await _start_claude(executable(tmp_path / "claude", "sleep 30"), _launch(tmp_path))
    sender, receiver = anyio.create_memory_object_stream[Outbound](8)
    replay: deque[str] = deque(maxlen=REPLAY_WINDOW)
    answered = ClaudeMessage(payload={"type": "assistant", "message": {"id": "msg_01abc"}}).model_dump_json()
    try:
        first_console, first_runner = memory_websocket_pair()
        await sender.send(Outbound(text=answered, replayable=True))
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(partial(_serve_console, first_runner, process, receiver, replay))
            with anyio.fail_after(5):
                assert await first_console.receive_text() == answered
            await first_console.close()

        second_console, second_runner = memory_websocket_pair()
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(partial(_serve_console, second_runner, process, receiver, replay))
            with anyio.fail_after(5):
                assert await second_console.receive_text() == answered, "the adopting console got nothing"
            await second_console.close()
    finally:
        sender.close()
        await _shutdown(process)


async def test_a_delta_is_sent_but_never_replayed(tmp_path: Path) -> None:
    """The one class replay corrupts: a `stream_event` has no identity for a console to recognise
    it by, and `streamed += delta` double-appends. It is also the class that never needs it —
    whatever it built is superseded by the completed `assistant` frame behind it."""
    process = await _start_claude(executable(tmp_path / "claude", "sleep 30"), _launch(tmp_path))
    sender, receiver = anyio.create_memory_object_stream[Outbound](8)
    replay: deque[str] = deque(maxlen=REPLAY_WINDOW)
    delta = ClaudeMessage(payload={"type": "stream_event", "event": {}}).model_dump_json()
    try:
        console, runner = memory_websocket_pair()
        await sender.send(Outbound(text=delta, replayable=False))
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(partial(_serve_console, runner, process, receiver, replay))
            with anyio.fail_after(5):
                assert await console.receive_text() == delta, "a delta must still reach the console live"
            await console.close()

        assert not replay, "a delta must not be retained for the next console"
    finally:
        sender.close()
        await _shutdown(process)


def executable(path: Path, body: str) -> Path:
    path.write_text(f"#!/usr/bin/env bash\n{body}\n")
    path.chmod(0o755)
    return path


def _launch(cwd: Path) -> ClaudeLaunch:
    return ClaudeLaunch(arguments=(), cwd=str(cwd), environment={})


async def test_workspace_setup_runs_in_the_launch_directory(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    setup = executable(tmp_path / "setup.sh", "pwd > marker")

    await prepare_workspace(setup, cwd=str(workspace))

    assert (workspace / "marker").read_text().strip() == str(workspace)


async def test_workspace_setup_streams_its_output_verbatim(tmp_path: Path) -> None:
    """The runner is a pipe: raw bytes, stderr included, no decoding and no line-splitting."""
    console_socket, runner_socket = memory_websocket_pair()
    # \xff is not valid UTF-8. The previous decode-in-the-runner design replaced it with
    # U+FFFD before the console ever saw it; nothing here is allowed to touch it.
    setup = executable(tmp_path / "setup.sh", r"printf 'cloning\n\xff\n'" + "\necho 'trouble' >&2")

    await prepare_workspace(setup, cwd=str(tmp_path), websocket=runner_socket)
    await runner_socket.close()

    forwarded = b""
    with contextlib.suppress(EOFError):
        while True:
            frame = RUNNER_TO_CONSOLE.validate_json(await console_socket.receive_text())
            assert isinstance(frame, SetupOutput)
            forwarded += frame.data
    assert forwarded == b"cloning\n\xff\ntrouble\n"


async def test_workspace_setup_failure_is_fatal(tmp_path: Path) -> None:
    """No checkout means no manual, and a Claude that starts anyway is a generic assistant."""
    setup = executable(tmp_path / "setup.sh", "echo 'no credential' >&2; exit 3")

    with pytest.raises(RuntimeError, match="exited with status 3"):
        await prepare_workspace(setup, cwd=str(tmp_path))


if __name__ == "__main__":
    pytest_bazel.main()
