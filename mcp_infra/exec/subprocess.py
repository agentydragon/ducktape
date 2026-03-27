from __future__ import annotations

import asyncio
import os
from pathlib import Path

from pydantic import Field

from mcp_infra.exec.models import (
    BaseExecResult,
    EnvVar,
    ExecArgsBase,
    ExecOutcome,
    ExecOutput,
    Exited,
    Killed,
    TimedOut,
    async_timer,
    render_outcome_to_result,
)


async def run_proc(
    argv: list[str],
    timeout_s: float,
    *,
    cwd: Path | None = None,
    stdin: bytes | str | None = None,
    env: dict[str, str] | None = None,
    inherit_env: bool = True,
) -> ExecOutcome:
    if stdin is None:
        stdin_bytes: bytes | None = None
    elif isinstance(stdin, str):
        stdin_bytes = stdin.encode("utf-8", errors="replace")
    else:
        stdin_bytes = stdin

    proc_env = dict(os.environ) if inherit_env else {}
    if env:
        proc_env.update(env)

    async with async_timer() as get_duration_ms:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
            env=proc_env,
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


# =============================================================================
# High-level exec interface (MCP-free)
# =============================================================================


class DirectExecArgs(ExecArgsBase):
    """Arguments for direct command execution."""

    cmd: list[str] = Field(min_length=1)
    env: list[EnvVar] | None = Field(
        default=None,
        description="Environment variables as ['NAME=value', ...]. "
        "Merged into the inherited environment by default (see inherit_env).",
    )
    inherit_env: bool = Field(
        default=True,
        description="If true (default), env vars are merged into the process's inherited environment. "
        "If false, only the provided env vars are used (clean environment).",
    )


def _parse_env_list(env_list: list[str] | None) -> dict[str, str] | None:
    if not env_list:
        return None
    result: dict[str, str] = {}
    for entry in env_list:
        name, sep, value = entry.partition("=")
        if not sep:
            raise ValueError(f"invalid env var (missing '='): {entry!r}")
        result[name] = value
    return result


async def run_direct_exec(input: DirectExecArgs, *, default_cwd: Path | None = None) -> BaseExecResult:
    """Execute a command locally (no sandbox).

    Standalone function for direct command execution. Can be called directly
    for in-container agent loops without MCP infrastructure.

    Args:
        input: Exec arguments (command, timeout, etc.)
        default_cwd: Fallback working directory if input.cwd is not specified
    """
    cwd_val: Path | None = Path(input.cwd) if input.cwd else None
    if cwd_val is None and default_cwd is not None:
        cwd_val = default_cwd

    timeout_s = max(0.001, input.timeout_ms / 1000.0)
    outcome = await run_proc(
        input.cmd,
        timeout_s,
        cwd=cwd_val,
        stdin=input.stdin_text,
        env=_parse_env_list(input.env),
        inherit_env=input.inherit_env,
    )
    return render_outcome_to_result(outcome, input.max_bytes)
