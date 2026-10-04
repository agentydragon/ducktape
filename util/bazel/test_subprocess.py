"""Tests for util.bazel.subprocess module."""

from pathlib import Path

import pytest_bazel

from util.bazel.subprocess import _in_bazel_venv, python_env, run_python_module


def test_python_env_inherit_includes_path():
    env = python_env(inherit=True)
    assert "PATH" in env


def test_python_env_no_inherit_minimal():
    env = python_env(inherit=False)
    assert "PATH" not in env


def test_python_env_bazel_venv_omits_pythonpath():
    """In a Bazel venv, PYTHONPATH is omitted regardless of ``inherit``.

    The child activates the venv via ``pyvenv.cfg`` discovery (``sys.executable``
    points at the venv python), so propagating PYTHONPATH is both unnecessary and
    harmful — it leaks the rules_python bootstrap's poisoned ``sys.path[0]``
    (the test's package directory) and causes stdlib shadowing.
    """
    assert _in_bazel_venv(), "test must run in a Bazel venv"
    assert "PYTHONPATH" not in python_env(inherit=True)
    assert "PYTHONPATH" not in python_env(inherit=False)


def test_python_env_sets_safe_path():
    """PYTHONSAFEPATH=1 prevents subprocesses from importing from CWD."""
    env = python_env(inherit=False)
    assert env.get("PYTHONSAFEPATH") == "1"


def test_subprocess_does_not_import_from_cwd(tmp_path: Path):
    """Subprocess launched via run_python_module cannot import from CWD.

    Regression test: without PYTHONSAFEPATH, `python -m module` prepends ''
    (CWD) to sys.path, causing it to shadow installed packages with source
    tree modules — e.g. the hook daemon imported from the git checkout
    instead of the Nix wheel.
    """
    # Create a decoy module in a temp dir that would shadow a stdlib module
    (tmp_path / "json.py").write_text("raise RuntimeError('imported from CWD!')\n")
    result = run_python_module("json.tool", "--help", capture_output=True, text=True, check=False, cwd=tmp_path)
    # json.tool --help should succeed (exit 0) using the real stdlib json,
    # not the decoy. Without PYTHONSAFEPATH, it would import the decoy and crash.
    assert result.returncode == 0, f"Subprocess imported from CWD: {result.stderr}"


def test_run_python_module_version():
    result = run_python_module("platform", capture_output=True, text=True, check=False)
    # python -m platform prints platform info
    assert result.returncode == 0
    assert result.stdout.strip()  # Should have output


if __name__ == "__main__":
    pytest_bazel.main()
