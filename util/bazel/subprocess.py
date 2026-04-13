"""Run Python modules as subprocesses under Bazel's rules_python.

Provides subprocess.run / asyncio.create_subprocess_exec wrappers and a
shell-wrapper generator for spawning Python module subprocesses.

In a Bazel venv (bootstrap_impl=script), subprocesses automatically get
correct sys.path via the venv's _bazel_site_init — no PYTHONPATH needed.
Outside Bazel (Nix wheel, hook daemon), PYTHONPATH is propagated so
subprocesses can find Nix-managed packages.
"""

from __future__ import annotations

import asyncio
import functools
import os
import shlex
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any


@functools.cache
def _in_bazel_venv() -> bool:
    """True if running inside a Bazel-managed venv (bootstrap_impl=script).

    In a Bazel venv, subprocesses spawned via sys.executable inherit the venv
    (pyvenv.cfg → site.py → _bazel_site_init) and get correct sys.path without
    PYTHONPATH. Propagating PYTHONPATH from the parent is harmful: the
    rules_python bootstrap prepends the test's package directory to sys.path[0],
    which leaks into PYTHONPATH and causes stdlib module shadowing (e.g., a
    local subprocess.py found before the stdlib one).
    """
    try:
        import _bazel_site_init  # type: ignore[import-untyped]  # noqa: F401, PLC0415

        return True
    except ImportError:
        return False


def _merge_pythonpath() -> str:
    """Merge sys.path with existing PYTHONPATH for non-Bazel contexts.

    Used by Nix-installed wheels (hook daemon, shims) where the subprocess
    doesn't have a Bazel venv. Merges sys.path (includes Nix site.addsitedir
    paths) with os.environ PYTHONPATH.
    """
    existing = os.environ.get("PYTHONPATH", "").split(os.pathsep) if os.environ.get("PYTHONPATH") else []
    merged: list[str] = []
    seen: set[str] = set()
    for p in [*sys.path, *existing]:
        if p and p not in seen:
            seen.add(p)
            merged.append(p)
    return os.pathsep.join(merged)


def python_env(*, inherit: bool = True) -> dict[str, str]:
    """Environment dict for spawning Python subprocesses.

    In a Bazel venv with ``inherit=True``, omits PYTHONPATH — the child
    inherits the venv (pyvenv.cfg → _bazel_site_init) and gets correct
    sys.path automatically.

    PYTHONPATH is still propagated when:
    - ``inherit=False``: minimal env, child needs explicit paths since
      env vars like RUNFILES_DIR (needed by _bazel_site_init) are missing.
    - Outside Bazel (Nix wheel): no venv, child needs PYTHONPATH to find
      Nix-managed packages.
    """
    env = os.environ.copy() if inherit else {}
    if _in_bazel_venv() and inherit:
        # Venv activation handles sys.path in the child. Don't propagate
        # PYTHONPATH — it includes the bootstrap's sys.path[0] prepend
        # which causes stdlib shadowing.
        env.pop("PYTHONPATH", None)
    else:
        env["PYTHONPATH"] = _merge_pythonpath()
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
