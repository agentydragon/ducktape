from __future__ import annotations

import asyncio
import logging

from pydantic import BaseModel, ConfigDict

from ..tool_execution import ToolPayload, ToolSpec

logger = logging.getLogger(__name__)


class RunShellCommandArgs(BaseModel):
    command: str
    model_config = ConfigDict(extra="forbid")


class ShellCommandResult(BaseModel):
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool = False
    model_config = ConfigDict(extra="forbid")


def build_spec() -> ToolSpec:
    async def handler(args: RunShellCommandArgs) -> ToolPayload:
        return await _run_command(args.command)

    return ToolSpec(
        name="run_shell_command",
        description="Execute a shell command inside the container (e.g., matrix CLI).",
        handler=handler,
    )


async def _run_command(command: str) -> ShellCommandResult:
    logger.info("Executing command: %s", command)
    proc = await asyncio.create_subprocess_shell(
        command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=60.0)
    except asyncio.TimeoutError:  # pragma: no cover - safety guard
        proc.kill()
        stdout, stderr = await proc.communicate()
        stdout = stdout or b""
        stderr = stderr or b""
        return ShellCommandResult(
            exit_code=124, stdout=_decode_stream(stdout), stderr=_decode_stream(stderr), timed_out=True
        )

    stdout_text = _decode_stream(stdout)
    stderr_text = _decode_stream(stderr)
    return ShellCommandResult(exit_code=proc.returncode or 0, stdout=stdout_text, stderr=stderr_text)


def _decode_stream(payload: bytes, limit: int = 4000) -> str:
    return payload.decode("utf-8", errors="replace")[:limit]
