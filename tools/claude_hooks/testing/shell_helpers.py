"""Shared utilities for running shell commands and locating runfiles in tests."""

from __future__ import annotations

import subprocess
from pathlib import Path

# Runfiles paths for binaries
SESSION_START = "_main/tools/claude_hooks/session_start"


def run_with_env_file(
    command: str,
    env_file: Path,
    cwd: Path | None = None,
    check: bool = False,
    capture_output: bool = True,
    text: bool = True,
    timeout: float | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run command in bash with env_file sourced (mimics Claude Code behavior)."""
    # Source the env file, then run the command
    bash_command = f"source {env_file} && {command}"

    return subprocess.run(
        ["bash", "-c", bash_command],
        cwd=cwd,
        check=check,
        capture_output=capture_output,
        text=text,
        timeout=timeout,
        env=env,
    )
