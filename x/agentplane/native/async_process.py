"""Async stdin/stdout/stderr pipes for native harness behavior tests."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from x.agentplane.native.process import text, text_record, write_jsonl

FrameResponder = Callable[[dict[str, Any]], Awaitable[BaseModel | None]]

# Tool results ride inside single frames, so a line can run to megabytes. Keep the test transport
# aligned with the runner, otherwise a behavior test can fail before exercising the harness.
_LINE_LIMIT = 64 * 1024 * 1024


class NativeProcessEofError(RuntimeError):
    """The native harness closed stdout before the awaited frame arrived."""


class FrameCursor:
    """One non-destructive reader of a native process's ordered stdout trace."""

    def __init__(self, trace: _FrameTrace, position: int):
        self._trace = trace
        self._position = position

    async def next(self) -> dict[str, Any]:
        self._position, frame = await self._trace.next(self._position)
        return frame


class _FrameTrace:
    """The append-only native stdout trace; cursors never consume one another's receipts."""

    def __init__(self) -> None:
        self._frames: list[dict[str, Any]] = []
        self._changed = asyncio.Event()
        self._closed = False
        self._failure: BaseException | None = None

    def cursor(self) -> FrameCursor:
        return FrameCursor(self, len(self._frames))

    async def append(self, frame: dict[str, Any]) -> None:
        self._frames.append(frame)
        self._changed.set()

    async def close(self, failure: BaseException | None = None) -> None:
        self._closed = True
        self._failure = failure
        self._changed.set()

    async def next(self, position: int) -> tuple[int, dict[str, Any]]:
        while True:
            if position < len(self._frames):
                return position + 1, self._frames[position]
            if self._failure is not None:
                raise self._failure
            if self._closed:
                raise NativeProcessEofError("native harness stdout closed before the awaited frame arrived")
            changed = self._changed
            changed.clear()
            if position < len(self._frames) or self._closed or self._failure is not None:
                continue
            await changed.wait()


class AsyncNativeProcess:
    """The native frame pipe, recording an ordered trace for independent test readers."""

    def __init__(self, logs: Path, command: list[str], *, cwd: Path, environment: dict[str, str]):
        self.logs, self.command, self.cwd, self.environment = logs, command, cwd, environment
        self.process: asyncio.subprocess.Process | None = None
        self._frames = _FrameTrace()
        self._stdout_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._stdin_lock = asyncio.Lock()
        self.frame_responder: FrameResponder | None = None

    async def __aenter__(self) -> AsyncNativeProcess:
        self.process = await asyncio.create_subprocess_exec(
            *self.command,
            cwd=self.cwd,
            env=self.environment,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
            limit=_LINE_LIMIT,
        )
        self._stdout_task = asyncio.create_task(self._stdout())
        self._stderr_task = asyncio.create_task(self._stderr())
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    async def send(self, frame: BaseModel) -> None:
        await self.send_many([frame])

    async def send_many(self, frames: list[BaseModel]) -> None:
        assert self.process is not None
        assert self.process.stdin is not None
        payloads = [frame.model_dump_json(by_alias=True).encode() for frame in frames]
        for payload in payloads:
            write_jsonl(self.logs / "stdin.jsonl", text_record(payload))
        if not payloads:
            return
        async with self._stdin_lock:
            self.process.stdin.write(b"".join(payload + b"\n" for payload in payloads))
            await self.process.stdin.drain()

    def frames(self) -> FrameCursor:
        """Start observing future stdout frames without consuming another observer's trace."""
        return self._frames.cursor()

    def alive(self) -> bool:
        return self.process is not None and self.process.returncode is None

    async def crash(self) -> int:
        assert self.process is not None
        self.process.kill()
        return await self.process.wait()

    async def close(self) -> int | None:
        if self.process is None:
            return None
        if self.process.stdin is not None:
            self.process.stdin.close()
            await self.process.stdin.wait_closed()
        try:
            result = await self.process.wait()
        except asyncio.CancelledError:
            self.process.terminate()
            await self.process.wait()
            raise
        for task in (self._stdout_task, self._stderr_task):
            if task is not None:
                await task
        return result

    def stdout_frames(self) -> list[dict[str, Any]]:
        records = (self.logs / "stdout.jsonl").read_text().splitlines()
        return [json.loads(json.loads(line)["text"]) for line in records]

    async def _stdout(self) -> None:
        assert self.process is not None
        assert self.process.stdout is not None
        try:
            while line := await self.process.stdout.readline():
                value = text(line.rstrip(b"\r\n"))
                write_jsonl(self.logs / "stdout.jsonl", text_record(value.encode()))
                frame = json.loads(value)
                if not isinstance(frame, dict):
                    raise ValueError("native stdout frame must be a JSON object")
                await self._frames.append(frame)
                if self.frame_responder is not None:
                    response = await self.frame_responder(frame)
                    if response is not None:
                        await self.send(response)
        except BaseException as error:
            await self._frames.close(error)
            raise
        else:
            await self._frames.close()

    async def _stderr(self) -> None:
        assert self.process is not None
        assert self.process.stderr is not None
        while chunk := await self.process.stderr.read(65536):
            write_jsonl(self.logs / "stderr.jsonl", text_record(chunk))
