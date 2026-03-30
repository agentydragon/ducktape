"""Run Python modules as subprocesses under Bazel's rules_python.

rules_python's venv-based bootstrap sets up sys.path via a virtual environment,
but that doesn't carry over to subprocesses spawned via sys.executable. This
module provides subprocess.run / asyncio.create_subprocess_exec wrappers and a
shell-wrapper generator that propagate PYTHONPATH automatically.
"""

from __future__ import annotations

import asyncio
import os
import shlex
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any


def python_env(*, inherit: bool = True) -> dict[str, str]:
    """Environment dict with PYTHONPATH propagated for Bazel subprocesses.

    Args:
        inherit: If True, start from os.environ. If False, return a minimal
                 dict with only PYTHONPATH.
    """
    env = os.environ.copy() if inherit else {}
    # Merge sys.path (includes Nix site.addsitedir paths) with existing PYTHONPATH.
    # Using only os.environ["PYTHONPATH"] loses sys.path entries added at runtime
    # (e.g., Nix wrapper's site.addsitedir); using only sys.path loses env entries.
    existing = os.environ.get("PYTHONPATH", "").split(os.pathsep) if os.environ.get("PYTHONPATH") else []
    merged: list[str] = []
    seen: set[str] = set()
    for p in [*sys.path, *existing]:
        if p and p not in seen:
            seen.add(p)
            merged.append(p)
    env["PYTHONPATH"] = os.pathsep.join(merged)
    # Prevent Python from prepending CWD to sys.path (equivalent to -P flag).
    # Without this, `python -m module` adds '' to sys.path[0], causing the
    # subprocess to import from the working directory instead of PYTHONPATH —
    # e.g. the hook daemon picks up source-tree modules instead of the
    # Nix-installed wheel, creating client/server version skew.
    env["PYTHONSAFEPATH"] = "1"
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


def exports_from_dict(env: Mapping[str, str | Path]) -> list[str]:
    """Generate shell export lines from an env var mapping.

    Values are shell-escaped with shlex.quote() to handle special characters.
    Accepts both str and Path values.
    """
    return [f"export {name}={shlex.quote(str(value))}" for name, value in env.items()]


def generate_shell_wrapper(
    module: str, *, baked_env: dict[str, str | Path] | None = None, extra_lines: str = ""
) -> str:
    """Generate a ``#!/bin/sh`` script that invokes ``sys.executable -m <module>``.

    Bakes PYTHONPATH (and any additional ``baked_env`` entries) into the script so
    subprocesses can find packages without leaking state via a shared env file.

    Args:
        module: Python module path (e.g. ``"devinfra.claude.bazel_wrapper"``).
        baked_env: Extra env vars to export before the exec (merged after PYTHONPATH).
        extra_lines: Raw shell lines inserted after exports and before the ``exec``
                     (use for dynamic expressions like ``$(...)``).
    """
    pythonpath = python_env(inherit=False)["PYTHONPATH"]
    env: dict[str, str | Path] = {"PYTHONPATH": pythonpath}
    if baked_env:
        env.update(baked_env)
    parts = ["#!/bin/sh", *exports_from_dict(env)]
    if extra_lines:
        parts.append(extra_lines)
    parts.append(f'exec "{sys.executable}" -m {module} "$@"')
    return "\n".join(parts) + "\n"


def write_shell_wrapper(
    path: Path, module: str, *, baked_env: dict[str, str | Path] | None = None, extra_lines: str = ""
) -> Path:
    """Write a shell wrapper script and make it executable.

    See :func:`generate_shell_wrapper` for argument docs.
    Returns *path* for chaining.
    """
    content = generate_shell_wrapper(module, baked_env=baked_env, extra_lines=extra_lines)
    path.write_text(content)
    path.chmod(0o755)
    return path
