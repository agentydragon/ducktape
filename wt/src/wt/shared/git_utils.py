from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import os
from pathlib import Path
import subprocess


def build_sanitized_git_env(env: Mapping[str, str] | None = None) -> dict[str, str]:
    e = dict(os.environ if env is None else env)
    e.setdefault("GIT_TERMINAL_PROMPT", "0")
    e.setdefault("GIT_CONFIG_GLOBAL", "/dev/null")
    e.setdefault("GIT_CONFIG_SYSTEM", "/dev/null")
    e.setdefault("GIT_SSH_COMMAND", "ssh -o BatchMode=yes")
    return e


@dataclass(slots=True)
class GitRunOptions:
    check: bool = True
    capture_output: bool = True
    env: Mapping[str, str] | None = None
    input_data: bytes | None = None


def git_run(
    args: Sequence[str | os.PathLike[str]], cwd: Path | str, options: GitRunOptions | None = None
) -> subprocess.CompletedProcess:
    opts = options or GitRunOptions()
    cmd: list[str | os.PathLike[str]] = ["git", "-c", "core.hooksPath=", *args]
    return subprocess.run(
        cmd,
        cwd=cwd,
        check=opts.check,
        capture_output=opts.capture_output,
        env=build_sanitized_git_env(opts.env),
        input=opts.input_data,
    )
