"""A CLI driver for protocol probes: every frame printed, both directions, nothing filtered.

**Not a Bazel test, and never will be.** A probe needs a real Claude credential and makes real
model calls; what it produces is an observation to write into <../protocol.md>, not a pass/fail.
Run one wherever a credential exists — a `haku-claude` sandbox pod, or any box with a logged-in
CLI:

    python3 -m haku.cli_protocol.probes.hooks
    CLAUDE_BIN=/path/to/claude python3 -m haku.cli_protocol.probes.hooks

**Standard library only**, here and in every probe, so that stays true: the box with the
credential is usually not the box with Bazel, and a probe that needed the repo's dependency set
could not run where the question is.

Verbatim logging is the point rather than a convenience. Earlier probes printed a curated subset
and managed to be inconclusive twice over, because a frame missing from the output could not be
told apart from a frame missing from the wire.

This driver deliberately does **not** reuse `ClaudeCli`. That client refuses every inbound
control request, which is right for the console and useless here: half of what these probes
measure is what the CLI asks a client to do. It does implement the same `FrameChannel` shape, so
the channel a probe drives is the one production drives.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

CLI = os.environ.get("CLAUDE_BIN", "claude")

# Answers an inbound control request, or returns None to have it refused.
InboundHandler = Callable[[dict[str, Any]], Awaitable[dict[str, Any] | None]]


class SubprocessChannel:
    """A `FrameChannel` (see `haku.runtime.x.claude_bridge.cli_client`) over a local CLI."""

    def __init__(self, *args: str):
        self._args = args
        self._process: asyncio.subprocess.Process | None = None

    async def connect(self) -> None:
        self._process = await asyncio.create_subprocess_exec(
            CLI,
            "--print",
            "--input-format",
            "stream-json",
            "--output-format",
            "stream-json",
            "--verbose",
            *self._args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=sys.stderr,
        )

    @property
    def process(self) -> asyncio.subprocess.Process:
        if self._process is None:
            raise RuntimeError("channel not connected")
        return self._process

    async def write(self, data: str) -> None:
        assert self.process.stdin is not None
        self.process.stdin.write(data.encode())
        await self.process.stdin.drain()

    async def read_messages(self) -> AsyncIterator[dict[str, Any]]:
        assert self.process.stdout is not None
        while line := await self.process.stdout.readline():
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                print(f"[non-json] {line!r}", flush=True)

    async def close(self) -> None:
        if self._process is None:
            return
        assert self._process.stdin is not None
        self._process.stdin.close()
        self._process.terminate()


class Probe:
    """One CLI process, with the whole conversation on stdout."""

    def __init__(self, *args: str):
        self.channel = SubprocessChannel(*args)
        self.frames: list[dict[str, Any]] = []
        self.inbound: InboundHandler | None = None
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._answering: set[asyncio.Task[None]] = set()
        self._started = 0.0

    async def start(self) -> None:
        await self.channel.connect()
        self._started = time.monotonic()
        self._reader = asyncio.create_task(self._read())

    def _log(self, arrow: str, payload: dict[str, Any]) -> None:
        print(f"[{time.monotonic() - self._started:6.2f}s] {arrow} {json.dumps(payload)}", flush=True)

    async def _write(self, payload: dict[str, Any]) -> None:
        await self.channel.write(json.dumps(payload) + "\n")
        self._log(">>", payload)

    async def control(self, request: dict[str, Any], seconds: float = 90.0) -> dict[str, Any]:
        """One control request, awaited.

        An `error` response is returned rather than raised: a probe that provokes a rejection
        wants to print it.
        """
        request_id = f"req_{uuid.uuid4().hex[:12]}"
        pending: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = pending
        await self._write({"type": "control_request", "request_id": request_id, "request": request})
        try:
            async with asyncio.timeout(seconds):
                return await pending
        finally:
            self._pending.pop(request_id, None)

    async def prompt(self, text: str, *, command_uuid: str | None = None) -> None:
        payload: dict[str, Any] = {
            "type": "user",
            "message": {"role": "user", "content": text},
            "parent_tool_use_id": None,
        }
        if command_uuid is not None:
            payload["uuid"] = command_uuid
        await self._write(payload)

    async def _read(self) -> None:
        async for frame in self.channel.read_messages():
            self.frames.append(frame)
            self._log("<<", frame)
            match frame.get("type"):
                case "control_response":
                    # The correlation key is nested inside `response`, not beside it.
                    response = frame["response"]
                    if (pending := self._pending.get(response["request_id"])) and not pending.done():
                        pending.set_result(response)
                case "control_request":
                    answering = asyncio.create_task(self._answer(frame))
                    self._answering.add(answering)
                    answering.add_done_callback(self._answering.discard)

    async def _answer(self, frame: dict[str, Any]) -> None:
        answer = await self.inbound(frame) if self.inbound is not None else None
        subtype = (frame.get("request") or {}).get("subtype")
        response = (
            {"subtype": "success", "request_id": frame["request_id"], "response": answer}
            if answer is not None
            else {"subtype": "error", "request_id": frame["request_id"], "error": f"{subtype} unsupported"}
        )
        await self._write({"type": "control_response", "response": response})

    async def wait_for(self, kind: str, *, seconds: float = 180.0) -> dict[str, Any]:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            for frame in self.frames:
                if frame.get("type") == kind:
                    return frame
            await asyncio.sleep(0.1)
        raise TimeoutError(f"no {kind} frame in {seconds}s")

    def of_type(self, kind: str) -> list[dict[str, Any]]:
        return [frame for frame in self.frames if frame.get("type") == kind]

    def inbound_subtypes(self) -> list[str]:
        return [str((frame.get("request") or {}).get("subtype")) for frame in self.of_type("control_request")]

    async def stop(self) -> None:
        self._reader.cancel()
        await self.channel.close()


async def allow_every_tool(frame: dict[str, Any]) -> dict[str, Any] | None:
    """A `can_use_tool` answer for probes not measuring the permission path itself.

    Reaching this at all needs `--permission-prompt-tool stdio`; without it the CLI refuses a
    tool that needs approval and asks nobody.
    """
    request = frame.get("request") or {}
    if request.get("subtype") != "can_use_tool":
        return None
    return {"behavior": "allow", "updatedInput": request.get("input")}
