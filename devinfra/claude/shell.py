"""Shell subprocess utilities for Claude Code hooks."""

import asyncio
import shlex
import subprocess
from pathlib import Path


async def start_with_env_file(
    command: str, env_file: Path, cwd: Path | None = None, *, env: dict[str, str] | None = None
) -> asyncio.subprocess.Process:
    """Start a command in bash with env_file sourced. Returns the running process."""
    bash_command = f"source {shlex.quote(str(env_file))} && {command}"
    return await asyncio.create_subprocess_exec(
        "bash",
        "-c",
        bash_command,
        cwd=str(cwd) if cwd else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )


async def run_with_env_file(
    command: str, env_file: Path, cwd: Path | None = None, *, check: bool = False, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    """Run command in bash with env_file sourced, wait for completion.

    Callers should wrap with ``async with asyncio.timeout(...)`` if needed.
    """
    proc = await start_with_env_file(command, env_file, cwd=cwd, env=env)
    stdout_bytes, stderr_bytes = await proc.communicate()

    result = subprocess.CompletedProcess(
        args=command,
        returncode=proc.returncode or 0,
        stdout=stdout_bytes.decode() if stdout_bytes else "",
        stderr=stderr_bytes.decode() if stderr_bytes else "",
    )
    if check and result.returncode != 0:
        raise subprocess.CalledProcessError(result.returncode, result.args, result.stdout, result.stderr)
    return result
