"""Minimal WebSocket client for Anthropic's process_api.

process_api is the PID 1 binary in Claude Code's Firecracker VMs. It
exposes a WebSocket server on port 2024 for spawning and attaching to
processes inside the guest.

Protocol summary (from RE of process_api binary):
  1. Client sends JWT token as first text frame (eyJ... prefix)
  2. Client sends CreateProcess JSON
  3. Server responds with ProcessCreated/ProcessCreatedV2
  4. I/O forwarded as text frames (ExpectStdOut/ExpectStdErr) + binary data
  5. ProcessExited text frame when process terminates
"""

from __future__ import annotations

import base64
import json
import logging
from collections.abc import AsyncIterator
from typing import Any

import websockets.asyncio.client

from devinfra.firecracker.manager.protocol import (
    NOOP_TAGS,
    CreateProcess,
    Exited,
    InfraError,
    OOMKilled,
    ProcessEvent,
    ProcessExited,
    ProcessTimedOut,
    ServerMessageKey,
    ServerTag,
    Stderr,
    Stdout,
    TimedOut,
)

logger = logging.getLogger(__name__)

_DUMMY_JWT = "eyJ" + base64.urlsafe_b64encode(b'{"alg":"none"}').decode().rstrip("=") + ".e30.stub"


async def run_process(
    host: str,
    command: str,
    args: list[str] | None = None,
    *,
    port: int = 2024,
    env_vars: dict[str, str] | None = None,
    uid: int = 0,
    gid: int = 0,
    timeout_secs: int = 300,
    jwt: str = _DUMMY_JWT,
) -> AsyncIterator[ProcessEvent]:
    """Run a command via process_api, yielding output events.

    Yields Stdout/Stderr chunks as they arrive, then a terminal event
    (Exited, TimedOut, OOMKilled, or InfraError).
    """
    uri = f"ws://{host}:{port}"
    async with websockets.asyncio.client.connect(uri, max_size=2**20) as ws:
        await ws.send(jwt)

        msg = CreateProcess(name=command, args=args or [], uid=uid, gid=gid, timeout=timeout_secs, env_vars=env_vars)
        await ws.send(msg.model_dump_json(exclude_none=True))

        expecting_stdout = False
        expecting_stderr = False

        async for message in ws:
            if isinstance(message, bytes):
                if expecting_stdout:
                    yield Stdout(data=message)
                    expecting_stdout = False
                elif expecting_stderr:
                    yield Stderr(data=message)
                    expecting_stderr = False
                continue

            parsed = json.loads(message)

            if isinstance(parsed, str):
                if parsed == ServerTag.EXPECT_STDOUT:
                    expecting_stdout = True
                elif parsed == ServerTag.EXPECT_STDERR:
                    expecting_stderr = True
                elif parsed not in NOOP_TAGS:
                    logger.debug("Unknown tag: %s", parsed)
                continue

            if not isinstance(parsed, dict):
                continue

            if event := _parse_server_message(parsed):
                yield event
                if isinstance(event, Exited | TimedOut | OOMKilled | InfraError):
                    return


async def spawn_systemd(host: str, *, port: int = 2024, jwt: str = _DUMMY_JWT) -> None:
    """Start NixOS systemd inside the guest via process_api.

    Sends CreateProcess for /nix/var/nix/profiles/system/init, sets it
    as reattachable (so it survives WebSocket disconnect), and returns
    immediately without waiting for exit.
    """
    uri = f"ws://{host}:{port}"
    async with websockets.asyncio.client.connect(uri, max_size=2**20) as ws:
        await ws.send(jwt)

        msg = CreateProcess(name="/nix/var/nix/profiles/system/init", reattachable=True, allow_process_id_reuse=False)
        await ws.send(msg.model_dump_json(exclude_none=True))

        async for message in ws:
            if isinstance(message, bytes):
                continue
            parsed = json.loads(message)
            if isinstance(parsed, dict) and (
                ServerMessageKey.PROCESS_CREATED in parsed or ServerMessageKey.PROCESS_CREATED_V2 in parsed
            ):
                logger.info("systemd started via process_api on %s", host)
                return
            if isinstance(parsed, dict) and ServerMessageKey.INFRA_ERROR in parsed:
                raise RuntimeError(f"Failed to start systemd: {parsed[ServerMessageKey.INFRA_ERROR]}")

        raise RuntimeError("WebSocket closed before ProcessCreated received")


def _parse_server_message(msg: dict[str, Any]) -> ProcessEvent | None:
    """Parse a server→client JSON dict message into a ProcessEvent.

    Returns None for known non-event messages (ProcessCreated, ConnectionCapabilities).
    Raises for unknown message types.
    """
    key = next(iter(msg))
    match key:
        case ServerMessageKey.PROCESS_CREATED | ServerMessageKey.PROCESS_CREATED_V2:
            return None
        case ServerMessageKey.PROCESS_EXITED:
            exited = ProcessExited.model_validate(msg[key])
            return Exited(status=exited.status, details=exited.details)
        case ServerMessageKey.PROCESS_TIMED_OUT:
            timed_out = ProcessTimedOut.model_validate(msg[key])
            return TimedOut(timeout_secs=timed_out.timeout_secs, details=timed_out.details)
        case ServerMessageKey.PROCESS_OUT_OF_MEMORY | ServerMessageKey.CONTAINER_OUT_OF_MEMORY:
            return OOMKilled()
        case ServerMessageKey.INFRA_ERROR:
            return InfraError(message=str(msg[key]))
        case (
            ServerMessageKey.CONNECTION_CAPABILITIES
            | ServerMessageKey.ATTACHED_TO_PROCESS
            | ServerMessageKey.ATTACHED_TO_PROCESS_V2
        ):
            return None
        case _:
            raise ValueError(f"Unknown server message key: {key!r} in {msg!r}")
