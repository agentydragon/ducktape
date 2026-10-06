from __future__ import annotations

import os
import subprocess
from collections.abc import Sequence
from pathlib import Path


def build_sanitized_git_env() -> dict[str, str]:
    e = dict(os.environ)
    e.setdefault("GIT_TERMINAL_PROMPT", "0")
    e.setdefault("GIT_CONFIG_GLOBAL", "/dev/null")
    e.setdefault("GIT_CONFIG_SYSTEM", "/dev/null")
    e.setdefault("GIT_SSH_COMMAND", "ssh -o BatchMode=yes")
    return e


def git_run(args: Sequence[str | os.PathLike[str]], cwd: Path | str) -> subprocess.CompletedProcess:
    cmd: list[str | os.PathLike[str]] = ["git", "-c", "core.hooksPath=", *args]
    return subprocess.run(cmd, cwd=cwd, check=True, capture_output=True, env=build_sanitized_git_env())
