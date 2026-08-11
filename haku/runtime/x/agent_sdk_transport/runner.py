"""Thin sandbox bridge between a WebSocket and a local Claude Code CLI."""

from __future__ import annotations

import argparse
import os
import subprocess
from collections.abc import AsyncIterator
from pathlib import Path

import anyio
from websockets.asyncio.client import ClientConnection, connect

from haku.runtime.x.agent_sdk_transport.protocol import (
    CONSOLE_TO_RUNNER,
    ClaudeLaunch,
    ClaudeMessage,
    EndInput,
    Progress,
    TextWebSocket,
    decode_object,
    encode_object,
)


class ClientWebSocketAdapter(TextWebSocket):
    """Adapt websockets' client connection to the transport's text-only surface."""

    def __init__(self, connection: ClientConnection):
        self._connection = connection

    async def send_text(self, data: str) -> None:
        await self._connection.send(data)

    async def receive_text(self) -> str:
        data = await self._connection.recv()
        if not isinstance(data, str):
            raise ValueError("Agent SDK transport requires text WebSocket frames")
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


async def _forward_cli_line(websocket: TextWebSocket, line: bytes) -> None:
    """Wrap one CLI stream-JSON line in a `claude` envelope, skipping anything that is not one."""
    if not (stripped := line.strip()).startswith(b"{"):
        return
    await websocket.send_text(ClaudeMessage(payload=decode_object(stripped.decode())).model_dump_json())


async def _send_cli_output(websocket: TextWebSocket, stdout: anyio.abc.ByteReceiveStream) -> None:
    pending = b""
    async for chunk in stdout:
        pending += chunk
        while b"\n" in pending:
            line, pending = pending.split(b"\n", 1)
            await _forward_cli_line(websocket, line)

    await _forward_cli_line(websocket, pending)


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
                # one: it comes once, before this loop, so a second means the console thinks
                # it is talking to a runner that has not launched. The types cannot say that,
                # so this check stays where the two above went.
                raise ValueError("console sent a second launch frame mid-conversation")


async def bridge_websocket_to_claude(websocket: TextWebSocket, *, claude_path: Path, launch: ClaudeLaunch) -> None:
    """Run one Claude CLI and copy its native stream-JSON protocol over WebSocket."""
    process = await anyio.open_process(
        build_claude_command(claude_path, launch),
        cwd=launch.cwd,
        env=build_claude_environment(launch),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    stdin = process.stdin
    stdout = process.stdout
    assert stdin is not None
    assert stdout is not None

    try:
        async with anyio.create_task_group() as tasks:

            async def websocket_to_cli() -> None:
                try:
                    await _send_websocket_input(websocket, stdin)
                except (EOFError, anyio.EndOfStream):
                    await stdin.aclose()
                    tasks.cancel_scope.cancel()

            async def cli_to_websocket() -> None:
                try:
                    await _send_cli_output(websocket, stdout)
                finally:
                    tasks.cancel_scope.cancel()

            tasks.start_soon(websocket_to_cli)
            tasks.start_soon(cli_to_websocket)

        return_code = await process.wait()
        if return_code != 0:
            raise RuntimeError(f"Claude Code exited with status {return_code}")
    finally:
        if process.returncode is None:
            process.terminate()
        with anyio.move_on_after(5, shield=True):
            await process.wait()
        if process.returncode is None:
            process.kill()
            await process.wait()
        await websocket.close()


async def prepare_workspace(setup_path: Path, *, cwd: str, websocket: TextWebSocket | None = None) -> None:
    """Run the shared sandbox bootstrap: git credentials and Haku's own checkouts.

    The same script the haku-sandbox exec target runs — see
    <../../../../cluster/k8s/haku/workspaces/image/haku-sandbox-setup.sh> — so this box gets
    the same `.netrc` and the same haku-state working copy rather than a second
    implementation that drifts from it.

    Run here, in the runner, rather than as an image entrypoint wrapper, so that `websocket`
    exists to narrate it: a clone is the longest thing between "provisioning" and an answer,
    and the console cannot report a step it cannot see.

    Every line it prints is forwarded and also echoed to this process's own stdout, so the pod
    log keeps the same record the room gets. Streaming the whole thing rather than a marked
    subset is deliberate — see `Progress`.

    **Fatal on failure.** Without the checkout the session has no manual, and a Claude Code
    that starts anyway is the generic-assistant failure the system prompt exists to prevent —
    silent, and indistinguishable from Haku having a bad day.
    """
    process = await anyio.open_process(
        [str(setup_path)], cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT
    )
    assert process.stdout is not None
    async for line in _lines(process.stdout):
        print(line, flush=True)
        # Skipping blanks only: a script that spaces its output would otherwise post empty
        # notices into the room.
        if websocket is not None and line.strip():
            await websocket.send_text(Progress(line=line.rstrip()).model_dump_json())
    if (status := await process.wait()) != 0:
        raise RuntimeError(f"workspace setup {setup_path} exited with status {status}")


async def _lines(stream: anyio.abc.ByteReceiveStream) -> AsyncIterator[str]:
    """Decoded, newline-delimited output, including a final unterminated line."""
    pending = b""
    async for chunk in stream:
        pending += chunk
        while b"\n" in pending:
            line, pending = pending.split(b"\n", 1)
            yield line.decode(errors="replace")
    if pending:
        yield pending.decode(errors="replace")


async def run(websocket_url: str, claude_path: Path, bearer_token: str | None, setup_path: Path | None = None) -> None:
    """Connect to Console and proxy its native SDK protocol to Claude Code."""
    headers: dict[str, str] | None = None
    if bearer_token:
        headers = {"Authorization": f"Bearer {bearer_token}"}

    async with connect(websocket_url, additional_headers=headers) as connection:
        websocket = ClientWebSocketAdapter(connection)
        if not isinstance(launch := CONSOLE_TO_RUNNER.validate_json(await websocket.receive_text()), ClaudeLaunch):
            raise ValueError(f"first bridge frame must be a launch, got {type(launch).__name__}")
        if setup_path is not None:
            await prepare_workspace(setup_path, cwd=launch.cwd, websocket=websocket)
        await bridge_websocket_to_claude(websocket, claude_path=claude_path, launch=launch)


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
