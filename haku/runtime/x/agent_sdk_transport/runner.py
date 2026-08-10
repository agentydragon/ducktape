"""Thin sandbox bridge between a WebSocket and a local Claude Code CLI."""

from __future__ import annotations

import argparse
import os
import subprocess
from pathlib import Path

import anyio
from websockets.asyncio.client import ClientConnection, connect

from haku.runtime.x.agent_sdk_transport.protocol import (
    END_INPUT_FRAME,
    ClaudeLaunch,
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


async def _send_cli_output(websocket: TextWebSocket, stdout: anyio.abc.ByteReceiveStream) -> None:
    pending = b""
    async for chunk in stdout:
        pending += chunk
        while b"\n" in pending:
            line, pending = pending.split(b"\n", 1)
            stripped = line.strip()
            if not stripped or not stripped.startswith(b"{"):
                continue
            await websocket.send_text(encode_object(decode_object(stripped.decode())))

    stripped = pending.strip()
    if stripped.startswith(b"{"):
        await websocket.send_text(encode_object(decode_object(stripped.decode())))


async def _send_websocket_input(websocket: TextWebSocket, stdin: anyio.abc.ByteSendStream) -> None:
    while True:
        frame = decode_object(await websocket.receive_text())
        if frame == END_INPUT_FRAME:
            await stdin.aclose()
            return
        await stdin.send((encode_object(frame) + "\n").encode())


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


async def run(websocket_url: str, claude_path: Path, bearer_token: str | None) -> None:
    """Connect to Console and proxy its native SDK protocol to Claude Code."""
    headers: dict[str, str] | None = None
    if bearer_token:
        headers = {"Authorization": f"Bearer {bearer_token}"}

    async with connect(websocket_url, additional_headers=headers) as connection:
        websocket = ClientWebSocketAdapter(connection)
        launch = ClaudeLaunch.from_frame(decode_object(await websocket.receive_text()))
        await bridge_websocket_to_claude(websocket, claude_path=claude_path, launch=launch)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Bridge a Haku Console WebSocket to Claude Code stdio.")
    parser.add_argument("--websocket-url", default=os.environ.get("HAKU_AGENT_SDK_RUNNER_WEBSOCKET_URL"))
    parser.add_argument("--session-id", default=os.environ.get("HAKU_CLAUDE_SESSION_ID"))
    parser.add_argument("--claude-path", type=Path, default=Path(os.environ.get("HAKU_CLAUDE_PATH", "claude")))
    args = parser.parse_args()
    if not args.websocket_url:
        parser.error("--websocket-url or HAKU_AGENT_SDK_RUNNER_WEBSOCKET_URL is required")
    if args.session_id:
        args.websocket_url = f"{args.websocket_url.rstrip('/')}/{args.session_id}"
    return args


def main() -> None:
    args = parse_args()
    bearer_token = os.environ.get("HAKU_AGENT_SDK_RUNNER_TOKEN")
    anyio.run(run, args.websocket_url, args.claude_path, bearer_token)


if __name__ == "__main__":
    main()
