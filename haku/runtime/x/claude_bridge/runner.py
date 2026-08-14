"""Thin sandbox bridge between a WebSocket and a local Claude Code CLI."""

from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
from pathlib import Path

import anyio
from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed

from haku.runtime.x.claude_bridge.protocol import (
    CONSOLE_TO_RUNNER,
    ClaudeLaunch,
    ClaudeMessage,
    EndInput,
    SetupOutput,
    TextWebSocket,
    decode_object,
    encode_object,
)

logger = logging.getLogger(__name__)

# How long the CLI's output waits for a console before its pipes fill and it pauses. Sized for a
# roll's worth of frames rather than a turn's: enough that a reconnect inside a few seconds costs
# nothing, small enough to be a buffer rather than a store.
OUTBOUND_BUFFER = 1000

RECONNECT_BASE_DELAY = 1.0
RECONNECT_MAX_DELAY = 20.0
# A sandbox held for a console that never returns is a wedged sandbox, which is worse than the
# wedged room it was protecting. Generous against a roll, decisive against an outage.
MAX_DISCONNECTED_SECONDS = 900.0

# The console's "you are not admitted": a consumed credential, a session already over. Nothing a
# retry can change, and retrying anyway is the crashloop this design exists to end.
POLICY_VIOLATION_CODE = 1008


class ClientWebSocketAdapter(TextWebSocket):
    """Adapt websockets' client connection to the transport's text-only surface."""

    def __init__(self, connection: ClientConnection):
        self._connection = connection

    async def send_text(self, data: str) -> None:
        await self._connection.send(data)

    async def receive_text(self) -> str:
        data = await self._connection.recv()
        if not isinstance(data, str):
            raise ValueError("the bridge requires text WebSocket frames")
        return data

    async def close(self) -> None:
        await self._connection.close()


def build_claude_command(claude_path: Path, launch: ClaudeLaunch) -> list[str]:
    """Prefix the trusted launch arguments with the sandbox-local CLI path."""
    return [str(claude_path), *launch.arguments]


def build_claude_environment(launch: ClaudeLaunch) -> dict[str, str]:
    """Overlay trusted launch values without exposing the bridge credential."""
    environment = {key: value for key, value in os.environ.items() if key != "HAKU_AGENT_SDK_RUNNER_TOKEN"}
    environment.update(
        {key: value for key, value in launch.environment.items() if key != "HAKU_AGENT_SDK_RUNNER_TOKEN"}
    )
    return environment


async def _queue_cli_line(outbound: MemoryObjectSendStream[str], line: bytes) -> None:
    """Wrap one CLI stream-JSON line in a `claude` envelope, skipping anything that is not one."""
    if not (stripped := line.strip()).startswith(b"{"):
        return
    await outbound.send(ClaudeMessage(payload=decode_object(stripped.decode())).model_dump_json())


async def _forward_cli_frames(outbound: MemoryObjectSendStream[str], stdout: anyio.abc.ByteReceiveStream) -> None:
    pending = b""
    async for chunk in stdout:
        pending += chunk
        while b"\n" in pending:
            line, pending = pending.split(b"\n", 1)
            await _queue_cli_line(outbound, line)

    await _queue_cli_line(outbound, pending)


async def _send_websocket_input(websocket: TextWebSocket, stdin: anyio.abc.ByteSendStream) -> None:
    while True:
        match CONSOLE_TO_RUNNER.validate_json(await websocket.receive_text()):
            case EndInput():
                await stdin.aclose()
                return
            case ClaudeMessage(payload=payload):
                await stdin.send((encode_object(payload) + "\n").encode())
            case ClaudeLaunch():
                # Not a direction error — `start` is the console's to send — but a sequencing
                # one: it opens a connection, so a second one mid-conversation means the console
                # thinks it is talking to a runner that has not launched. The types cannot say
                # that, so this check stays where the two above went.
                raise ValueError("console sent a second launch frame mid-conversation")


async def _forward_cli_errors(outbound: MemoryObjectSendStream[str], stderr: anyio.abc.ByteReceiveStream) -> None:
    """Forward what the CLI wrote to stderr, to this log and to the console.

    It used to go to `DEVNULL`, which is the one place a failure to start is explained: the
    console sees the exit status and nothing else, so `Claude Code exited with status 1` was the
    whole account of a rejected credential or a bad flag.

    Sent as `SetupOutput` because that frame is already "bytes the sandbox wrote" and adding a
    kind of its own would be a `PROTOCOL_VERSION` bump — which, until the two ends negotiate,
    breaks every session on release. The console narrates and records it like any other sandbox
    output; telling the two apart is worth a frame kind once one is affordable.
    """
    async for chunk in stderr:
        sys.stdout.buffer.write(chunk)
        sys.stdout.buffer.flush()
        await outbound.send(SetupOutput(data=chunk).model_dump_json())


async def _start_claude(claude_path: Path, launch: ClaudeLaunch) -> anyio.abc.Process:
    return await anyio.open_process(
        build_claude_command(claude_path, launch),
        cwd=launch.cwd,
        env=build_claude_environment(launch),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


async def _shutdown(process: anyio.abc.Process) -> int | None:
    """Stop the CLI, reporting the status it chose for itself — or None if we chose for it.

    The distinction is the difference between a failure and a teardown: a process we signalled
    reports the signal, so treating that as an exit status files every clean shutdown as
    `Claude Code exited with status -15`.
    """
    # A CLI that has already exited is reaped here, so its own status is the one reported.
    with anyio.move_on_after(1):
        await process.wait()
    exited_with = process.returncode

    if process.returncode is None:
        process.terminate()
    with anyio.move_on_after(5, shield=True):
        await process.wait()
    if process.returncode is None:
        process.kill()
        await process.wait()
    return exited_with


async def _drain_cli(
    process: anyio.abc.Process, outbound: MemoryObjectSendStream[str], scope: anyio.CancelScope
) -> None:
    """Read the CLI for as long as it lives, whether or not a console is listening.

    Long-lived rather than per-connection, which is what lets the process outlive a socket: its
    pipes keep draining into `outbound`, so a disconnected console costs the sandbox a pause
    rather than a wedge, and nothing it said is thrown away because there was nowhere to put it.
    When the buffer fills the reads stop, the pipes fill behind them, and the CLI waits — the
    honest behaviour for an agent nobody is listening to.

    Cancels *scope* when stdout ends, because that is the CLI exiting and there is then nothing
    left to serve any console with.
    """
    stdout, stderr = process.stdout, process.stderr
    assert stdout is not None
    assert stderr is not None
    async with anyio.create_task_group() as readers:
        # stderr ending says nothing about the conversation; stdout ending is the CLI's exit.
        readers.start_soon(_forward_cli_errors, outbound, stderr)
        await _forward_cli_frames(outbound, stdout)
        readers.cancel_scope.cancel()
    scope.cancel()


async def _serve_console(
    websocket: TextWebSocket, process: anyio.abc.Process, outbound: MemoryObjectReceiveStream[str]
) -> None:
    """Copy frames both ways for one console connection, returning when that connection ends.

    A frame taken from the buffer and then lost to a dying socket is gone: this delivers at most
    once, which is enough while adoption is limited to sessions between turns. Making a
    reconnect lossless is the resume cursor plus frame identity in
    <../../../plans/cli_protocol_ownership.md>, and it is the next stage of this work.
    """
    stdin = process.stdin
    assert stdin is not None
    async with anyio.create_task_group() as tasks:

        async def console_to_cli() -> None:
            try:
                await _send_websocket_input(websocket, stdin)
            except (EOFError, anyio.EndOfStream, ConnectionClosed):
                pass
            finally:
                tasks.cancel_scope.cancel()

        async def cli_to_console() -> None:
            try:
                async for frame in outbound:
                    await websocket.send_text(frame)
            except (ConnectionClosed, anyio.BrokenResourceError):
                pass
            finally:
                tasks.cancel_scope.cancel()

        tasks.start_soon(console_to_cli)
        tasks.start_soon(cli_to_console)


async def bridge_websocket_to_claude(websocket: TextWebSocket, *, claude_path: Path, launch: ClaudeLaunch) -> None:
    """Run one Claude CLI and serve exactly one console connection with it.

    The single-connection shape, kept because it is the whole of what a session without
    reconnection needs; `run` composes the same pieces with a process that outlives the socket.
    """
    outbound_sender, outbound_receiver = anyio.create_memory_object_stream[str](OUTBOUND_BUFFER)
    process = await _start_claude(claude_path, launch)
    try:
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(_drain_cli, process, outbound_sender, tasks.cancel_scope)
            await _serve_console(websocket, process, outbound_receiver)
            tasks.cancel_scope.cancel()
    finally:
        exited_with = await _shutdown(process)
        await websocket.close()
    if exited_with not in (0, None):
        raise RuntimeError(f"Claude Code exited with status {exited_with}")


async def prepare_workspace(setup_path: Path, *, cwd: str, websocket: TextWebSocket | None = None) -> None:
    """Run the shared sandbox bootstrap: git credentials and Haku's own checkouts.

    The same script the haku-sandbox exec target runs — see
    <../../../../cluster/k8s/haku/workspaces/image/haku-sandbox-setup.sh> — so this box gets
    the same `.netrc` and the same haku-state working copy rather than a second
    implementation that drifts from it.

    Run here, in the runner, rather than as an image entrypoint wrapper, so that `websocket`
    exists to narrate it: a clone is the longest thing between "provisioning" and an answer,
    and the console cannot report a step it cannot see.

    Its output is forwarded verbatim, in whatever chunks it arrives in, and written unchanged
    to this process's own stdout so the pod log keeps the same record the room gets. No
    decoding, no line-splitting, no filtering here — see `SetupOutput` for why that is the
    console's job.

    **Fatal on failure.** Without the checkout the session has no manual, and a Claude Code
    that starts anyway is the generic-assistant failure the system prompt exists to prevent —
    silent, and indistinguishable from Haku having a bad day.
    """
    process = await anyio.open_process(
        [str(setup_path)], cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT
    )
    assert process.stdout is not None
    async for chunk in process.stdout:
        # `sys.stdout.buffer`, not `print`: the local log gets the same bytes the console does,
        # rather than a decoded-and-maybe-replaced rendering of them.
        sys.stdout.buffer.write(chunk)
        sys.stdout.buffer.flush()
        if websocket is not None:
            await websocket.send_text(SetupOutput(data=chunk).model_dump_json())
    if (status := await process.wait()) != 0:
        raise RuntimeError(f"workspace setup {setup_path} exited with status {status}")


async def _receive_launch(websocket: TextWebSocket) -> ClaudeLaunch:
    if not isinstance(launch := CONSOLE_TO_RUNNER.validate_json(await websocket.receive_text()), ClaudeLaunch):
        raise ValueError(f"first bridge frame must be a launch, got {type(launch).__name__}")
    return launch


def _reconnect_delay(attempt: int) -> float:
    return min(RECONNECT_BASE_DELAY * 2.0**attempt, RECONNECT_MAX_DELAY)


async def run(websocket_url: str, claude_path: Path, bearer_token: str | None, setup_path: Path | None = None) -> None:
    """Serve one Claude CLI to whichever console is up, across as many connections as that takes.

    **The CLI outlives the connection.** It used to die with it — one socket, one process, and
    `bridge_websocket_to_claude` terminating Claude in its `finally` — so a console roll ended
    the conversation, and the reconnect Kubernetes then forced was refused by a console that
    admitted a session only once. Six rolls a day made that the normal end of a session.

    So this dials, serves, and dials again, holding the process across the gap. What the console
    sends on a later connection is a `start` frame it built fresh, and it is deliberately
    **ignored**: the launch decided the argv, the system prompt and the MCP wiring of a process
    that is already running, and none of those can be re-applied to it. The runner is the honest
    owner of "the handshake already happened", because it is the end that owns the process.

    Giving up is bounded. A sandbox held for a console that never returns trades a wedged room
    for a wedged sandbox, so after `MAX_DISCONNECTED_SECONDS` without one this exits and lets the
    claim be reclaimed.
    """
    headers: dict[str, str] | None = None
    if bearer_token:
        headers = {"Authorization": f"Bearer {bearer_token}"}

    outbound_sender, outbound_receiver = anyio.create_memory_object_stream[str](OUTBOUND_BUFFER)
    process: anyio.abc.Process | None = None
    attempt = 0
    gave_up_at = anyio.current_time() + MAX_DISCONNECTED_SECONDS

    try:
        async with anyio.create_task_group() as session:
            while True:
                try:
                    async with connect(websocket_url, additional_headers=headers) as connection:
                        websocket = ClientWebSocketAdapter(connection)
                        launch = await _receive_launch(websocket)
                        if process is None:
                            if setup_path is not None:
                                await prepare_workspace(setup_path, cwd=launch.cwd, websocket=websocket)
                            process = await _start_claude(claude_path, launch)
                            # Long-lived, so nothing the CLI writes is lost to a closed socket and
                            # its pipes never fill: they drain into the buffer either way, and the
                            # buffer's backpressure pauses the CLI rather than dropping what it said.
                            session.start_soon(_drain_cli, process, outbound_sender, session.cancel_scope)
                        attempt = 0
                        gave_up_at = anyio.current_time() + MAX_DISCONNECTED_SECONDS
                        await _serve_console(websocket, process, outbound_receiver)
                except ConnectionClosed as closed:
                    # 1008 is the console refusing this runner — a consumed credential, a session
                    # already over. Retrying cannot change either, and retrying anyway is the
                    # crashloop this whole design is undoing.
                    if closed.code == POLICY_VIOLATION_CODE:
                        logger.info("Console refused this runner (%s); stopping", closed.reason)
                        return
                except OSError as error:
                    logger.info("Console unreachable (%s); redialling", error)
                if anyio.current_time() >= gave_up_at:
                    logger.error("No console for %ds; giving up this sandbox", MAX_DISCONNECTED_SECONDS)
                    return
                await anyio.sleep(_reconnect_delay(attempt))
                attempt += 1
    finally:
        if process is not None and (exited_with := await _shutdown(process)) not in (0, None):
            raise RuntimeError(f"Claude Code exited with status {exited_with}")


def _optional_path(value: str | None) -> Path | None:
    return Path(value) if value else None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Bridge a Haku Console WebSocket to Claude Code stdio.")
    parser.add_argument("--websocket-url", default=os.environ.get("HAKU_AGENT_SDK_RUNNER_WEBSOCKET_URL"))
    parser.add_argument("--session-id", default=os.environ.get("HAKU_CLAUDE_SESSION_ID"))
    parser.add_argument("--claude-path", type=Path, default=Path(os.environ.get("HAKU_CLAUDE_PATH", "claude")))
    # Unset means "no bootstrap", which is what the transport's own tests and any bare
    # local run want; the image sets it.
    parser.add_argument("--setup-path", type=Path, default=_optional_path(os.environ.get("HAKU_CLAUDE_SETUP")))
    args = parser.parse_args()
    if not args.websocket_url:
        parser.error("--websocket-url or HAKU_AGENT_SDK_RUNNER_WEBSOCKET_URL is required")
    if args.session_id:
        args.websocket_url = f"{args.websocket_url.rstrip('/')}/{args.session_id}"
    return args


def main() -> None:
    args = parse_args()
    bearer_token = os.environ.get("HAKU_AGENT_SDK_RUNNER_TOKEN")
    anyio.run(run, args.websocket_url, args.claude_path, bearer_token, args.setup_path)


if __name__ == "__main__":
    main()
