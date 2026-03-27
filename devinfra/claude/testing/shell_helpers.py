"""Shared utilities for running shell commands and locating runfiles in tests."""

import asyncio
import subprocess
from pathlib import Path

# Runfiles paths for binaries
HOOK_DISPATCH = "_main/devinfra/claude/hook_daemon/hook_dispatch"


async def run_with_env_file(
    command: str, env_file: Path, cwd: Path | None = None, *, check: bool = False, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    """Run command in bash with env_file sourced (mimics Claude Code behavior).

    Callers should wrap with ``async with asyncio.timeout(...)`` if needed.
    """
    bash_command = f"source {env_file} && {command}"

    proc = await asyncio.create_subprocess_exec(
        "bash", "-c", bash_command, cwd=cwd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=env
    )
    stdout_bytes, stderr_bytes = await proc.communicate()

    result = subprocess.CompletedProcess(
        args=["bash", "-c", bash_command],
        returncode=proc.returncode or 0,
        stdout=stdout_bytes.decode() if stdout_bytes else "",
        stderr=stderr_bytes.decode() if stderr_bytes else "",
    )
    if check and result.returncode != 0:
        raise subprocess.CalledProcessError(result.returncode, result.args, result.stdout, result.stderr)
    return result
