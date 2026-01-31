"""Run Python modules as subprocesses under Bazel's rules_python.

rules_python's venv-based bootstrap sets up sys.path via a virtual environment,
but that doesn't carry over to subprocesses spawned via sys.executable. This
module provides subprocess.run / asyncio.create_subprocess_exec wrappers and a
shell-wrapper generator that propagate PYTHONPATH automatically.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path
from typing import Any


def python_env(*, inherit: bool = True) -> dict[str, str]:
    """Environment dict with PYTHONPATH propagated for Bazel subprocesses.

    Args:
        inherit: If True, start from os.environ. If False, return a minimal
                 dict with only PYTHONPATH.
    """
    env = os.environ.copy() if inherit else {}
    env["PYTHONPATH"] = os.environ.get("PYTHONPATH") or os.pathsep.join(sys.path)
    return env


def run_python_module(
    module: str, *args: str | os.PathLike[str], inherit_env: bool = True, **kwargs: Any
) -> subprocess.CompletedProcess[Any]:
    """Run ``sys.executable -m <module> <args>`` with PYTHONPATH propagated.

    Thin wrapper around :func:`subprocess.run`.  All *kwargs* are forwarded
    directly (``cwd``, ``timeout``, ``capture_output``, …).

    Args:
        module: Python module to run (e.g. ``"pre_commit"``, ``"ruff"``).
        *args: Command-line arguments passed after the module name.
               Accepts :class:`str` and :class:`os.PathLike`.
        inherit_env: If True, start from ``os.environ``. If False, minimal env
                     with only ``PYTHONPATH``.
    """
    cmd: list[str | os.PathLike[str]] = [sys.executable, "-m", module, *args]
    return subprocess.run(cmd, env=python_env(inherit=inherit_env), **kwargs)  # noqa: PLW1510  check forwarded via kwargs


async def async_run_python_module(
    module: str, *args: str | os.PathLike[str], inherit_env: bool = True, **kwargs: Any
) -> asyncio.subprocess.Process:
    """Async variant of :func:`run_python_module`.

    Returns an :class:`asyncio.subprocess.Process` (not awaited to completion).
    Caller is responsible for awaiting ``process.wait()`` or
    ``process.communicate()``.

    All *kwargs* are forwarded to :func:`asyncio.create_subprocess_exec`.
    """
    cmd: list[str] = [sys.executable, "-m", module, *(str(a) for a in args)]
    return await asyncio.create_subprocess_exec(*cmd, env=python_env(inherit=inherit_env), **kwargs)


def generate_shell_wrapper(module: str, *, extra_lines: str = "") -> str:
    """Generate a ``#!/bin/sh`` script that invokes ``sys.executable -m <module>``.

    The wrapper bakes the current PYTHONPATH into the script so the subprocess
    can find packages without leaking PYTHONPATH into every child process via
    a shared env file.

    Args:
        module: Python module path (e.g. ``"tools.claude_hooks.bazel_wrapper"``).
        extra_lines: Additional shell lines inserted before the ``exec``.
    """
    pythonpath = os.environ.get("PYTHONPATH") or os.pathsep.join(sys.path)
    parts = ["#!/bin/sh", f'export PYTHONPATH="{pythonpath}"']
    if extra_lines:
        parts.append(extra_lines)
    parts.append(f'exec "{sys.executable}" -m {module} "$@"')
    return "\n".join(parts) + "\n"


def write_shell_wrapper(path: Path, module: str, *, extra_lines: str = "") -> Path:
    """Write a shell wrapper script and make it executable.

    See :func:`generate_shell_wrapper` for argument docs.
    Returns *path* for chaining.
    """
    content = generate_shell_wrapper(module, extra_lines=extra_lines)
    path.write_text(content)
    path.chmod(0o755)
    return path
