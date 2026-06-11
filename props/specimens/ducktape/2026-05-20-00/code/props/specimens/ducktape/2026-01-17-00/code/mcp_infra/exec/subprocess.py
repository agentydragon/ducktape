from __future__ import annotations

import asyncio
from pathlib import Path

from mcp_infra.exec.models import ExecOutcome, ExecOutput, Exited, Killed, TimedOut, async_timer


async def run_proc(
    argv: list[str], timeout_s: float, *, cwd: Path | None = None, stdin: bytes | str | None = None
) -> ExecOutcome:
    if stdin is None:
        stdin_bytes: bytes | None = None
    elif isinstance(stdin, str):
        stdin_bytes = stdin.encode("utf-8", errors="replace")
    else:
        stdin_bytes = stdin

    async with async_timer() as get_duration_ms:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
        )

        try:
            out, err = await asyncio.wait_for(proc.communicate(input=stdin_bytes), timeout=timeout_s)

            stdout_bytes = out if out is not None else b""
            stderr_bytes = err if err is not None else b""
            exit_code = proc.returncode if proc.returncode is not None else 0
            output = ExecOutput(stdout=stdout_bytes, stderr=stderr_bytes)

            # Detect if process was killed by signal (negative exit code on Unix)
            if exit_code < 0:
                signal_num = -exit_code
                return ExecOutcome(output=output, exit=Killed(signal=signal_num), duration_ms=get_duration_ms())

            return ExecOutcome(output=output, exit=Exited(exit_code=exit_code), duration_ms=get_duration_ms())

        except TimeoutError:
            proc.kill()
            try:
                out, err = await asyncio.wait_for(proc.communicate(), timeout=5)
            except (TimeoutError, ProcessLookupError):
                out, err = b"", b""
            stdout_bytes = out if out is not None else b""
            stderr_bytes = err if err is not None else b""
            output = ExecOutput(stdout=stdout_bytes, stderr=stderr_bytes)
            return ExecOutcome(output=output, exit=TimedOut(), duration_ms=get_duration_ms())
