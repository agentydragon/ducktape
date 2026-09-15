"""One native harness child: its pipes, line framing, and exit. No protocol knowledge."""

from __future__ import annotations

import asyncio
import os
import signal
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from contextlib import suppress
from pathlib import Path

from util.bazel.runfiles import get_required_path, own_repo_rlocation

# Tool results ride inside single frames, so a line can run to megabytes.
_LINE_LIMIT = 64 * 1024 * 1024


class HarnessProcess:
    def __init__(
        self, command: Sequence[str], *, cwd: Path, environment: Mapping[str, str], state_owner_descriptor: int
    ) -> None:
        self.command = list(command)
        self.cwd = cwd
        self.environment = dict(environment)
        self.state_owner_descriptor = state_owner_descriptor
        self._process: asyncio.subprocess.Process | None = None
        self._native_pid = 0
        self._stdin_lock = asyncio.Lock()

    async def start(self) -> None:
        report_reader, report_writer = os.pipe()
        os.set_inheritable(report_writer, True)
        try:
            self._process = await asyncio.create_subprocess_exec(
                str(get_required_path(own_repo_rlocation("x/agentplane/runner/harness_supervisor"))),
                "--native-pid-fd",
                str(report_writer),
                *self.command,
                cwd=self.cwd,
                env=self.environment,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                # The supervisor keeps this descriptor while it reaps a harness after the runner
                # dies. A successor therefore cannot acquire state ownership until native work is
                # fenced, not just until the runner PID exits.
                pass_fds=(self.state_owner_descriptor, report_writer),
                # The harness's own tool subprocesses join its process group, so stopping the harness
                # stops everything it started. The supervisor places the native harness in a child
                # group and relays that stop before it exits.
                start_new_session=True,
                limit=_LINE_LIMIT,
            )
        finally:
            os.close(report_writer)
        try:
            reported = await asyncio.to_thread(os.read, report_reader, 32)
        finally:
            os.close(report_reader)
        try:
            self._native_pid = int(reported)
        except ValueError as error:
            await self._process.wait()
            raise RuntimeError(f"harness supervisor did not report a native pid: {reported!r}") from error

    @property
    def process(self) -> asyncio.subprocess.Process:
        if self._process is None:
            raise RuntimeError("harness process was not started")
        return self._process

    @property
    def native_pid(self) -> int:
        if self._native_pid == 0:
            raise RuntimeError("harness process was not started")
        return self._native_pid

    @property
    def running(self) -> bool:
        return self._process is not None and self._process.returncode is None

    async def write_line(self, line: str) -> None:
        stdin = self.process.stdin
        assert stdin is not None
        async with self._stdin_lock:
            stdin.write(line.encode() + b"\n")
            await stdin.drain()

    async def lines(self) -> AsyncIterator[str]:
        """stdout lines without their newline, until EOF."""
        stdout = self.process.stdout
        assert stdout is not None
        while line := await stdout.readline():
            yield line.rstrip(b"\r\n").decode()

    async def stderr_chunks(self) -> AsyncIterator[str]:
        stderr = self.process.stderr
        assert stderr is not None
        while chunk := await stderr.read(65536):
            yield chunk.decode(errors="replace")

    async def wait(self) -> int:
        return await self.process.wait()

    async def stop(self, *, grace_s: float = 5) -> int:
        """Close stdin, which both harnesses treat as end of session; escalate if that is not enough."""
        process = self.process
        if process.returncode is not None:
            return process.returncode
        if process.stdin is not None:
            with suppress(BrokenPipeError, ConnectionResetError):
                process.stdin.close()
        for send in (self._signal_group(signal.SIGTERM), self._signal_group(signal.SIGKILL)):
            try:
                return await asyncio.wait_for(process.wait(), timeout=grace_s)
            except TimeoutError:
                send()
        return await process.wait()

    def _signal_group(self, signum: int) -> Callable[[], None]:
        def send() -> None:
            with suppress(ProcessLookupError):
                os.killpg(self.native_pid, signum)
            with suppress(ProcessLookupError):
                os.killpg(self.process.pid, signum)

        return send
