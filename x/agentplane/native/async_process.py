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


class AsyncNativeProcess:
    """The native frame pipe, exposing ordered receipt rather than predicate waits."""

    def __init__(self, logs: Path, command: list[str], *, cwd: Path, environment: dict[str, str]):
        self.logs, self.command, self.cwd, self.environment = logs, command, cwd, environment
        self.process: asyncio.subprocess.Process | None = None
        self._frames: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
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

    async def next_frame(self) -> dict[str, Any]:
        return await self._frames.get()

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
        while line := await self.process.stdout.readline():
            value = text(line.rstrip(b"\r\n"))
            write_jsonl(self.logs / "stdout.jsonl", text_record(value.encode()))
            frame = json.loads(value)
            if not isinstance(frame, dict):
                raise ValueError("native stdout frame must be a JSON object")
            if self.frame_responder is not None:
                response = await self.frame_responder(frame)
                if response is not None:
                    await self.send(response)
            await self._frames.put(frame)

    async def _stderr(self) -> None:
        assert self.process is not None
        assert self.process.stderr is not None
        while chunk := await self.process.stderr.read(65536):
            write_jsonl(self.logs / "stderr.jsonl", text_record(chunk))
