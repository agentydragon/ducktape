"""Run Python modules as subprocesses under Bazel's rules_python.

Provides subprocess.run / asyncio.create_subprocess_exec wrappers for spawning
Python module subprocesses.

In a Bazel venv (bootstrap_impl=script), subprocesses automatically get
correct sys.path via the venv's _bazel_site_init — no PYTHONPATH needed.
Outside Bazel (Nix wheel, hook daemon), PYTHONPATH is propagated so
subprocesses can find Nix-managed packages.
"""

from __future__ import annotations

import asyncio
import functools
import os
import subprocess
import sys
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
        # gazelle:ignore _bazel_site_init
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


#: Environment variables that ``_bazel_site_init`` reads to locate runfiles
#: when a child venv Python is launched. Without at least one of these the
#: venv's ``bazel.pth`` site hook cannot resolve the runfiles root, so the
#: child fails to import any repo-local module.
_BAZEL_RUNFILES_ENV = ("RUNFILES_DIR", "RUNFILES_MANIFEST_FILE", "JAVA_RUNFILES", "TEST_SRCDIR")


def python_env(*, inherit: bool = True) -> dict[str, str]:
    """Environment dict for spawning Python subprocesses.

    In a Bazel venv, omits PYTHONPATH — ``sys.executable`` points at the venv
    python, so the child activates the venv via ``pyvenv.cfg`` discovery
    (``site.py`` → ``bazel.pth`` → ``_bazel_site_init``) and gets the correct
    sys.path without PYTHONPATH. With ``inherit=False`` we still forward the
    Bazel runfiles pointers so that venv activation can find the runfiles root.

    Outside Bazel (Nix wheel, hook daemon), PYTHONPATH is propagated so the
    child can find Nix-managed packages — there is no venv to activate.

    Propagating PYTHONPATH inside a Bazel venv is actively harmful: with
    ``--incompatible_default_to_explicit_init_py``, the rules_python bootstrap
    prepends the test's package directory to ``sys.path[0]``, which leaks into
    PYTHONPATH and causes stdlib shadowing when a local file collides with a
    stdlib module (e.g., ``util/exec/subprocess.py`` shadowing stdlib
    ``subprocess`` in any subprocess spawned from a test in that package).
    See <devinfra/debug/explicit_init_py_investigation.md>.
    """
    env = os.environ.copy() if inherit else {k: v for k in _BAZEL_RUNFILES_ENV if (v := os.environ.get(k)) is not None}
    if _in_bazel_venv():
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
