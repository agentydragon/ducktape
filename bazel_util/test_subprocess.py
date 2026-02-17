"""Tests for bazel_util.subprocess module."""

import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
import pytest_bazel

from bazel_util.subprocess import (
    exports_from_dict,
    generate_shell_wrapper,
    python_env,
    run_python_module,
    write_shell_wrapper,
)


def test_python_env_inherit_includes_pythonpath():
    env = python_env(inherit=True)
    assert "PYTHONPATH" in env
    # Should also contain other env vars
    assert "PATH" in env


def test_python_env_no_inherit_minimal():
    env = python_env(inherit=False)
    assert "PYTHONPATH" in env
    assert "PATH" not in env


def test_python_env_uses_existing_pythonpath():
    with patch.dict(os.environ, {"PYTHONPATH": "/custom/path"}):
        env = python_env(inherit=False)
        assert env["PYTHONPATH"] == "/custom/path"


def test_python_env_falls_back_to_sys_path():
    with patch.dict(os.environ, {}, clear=True):
        env = python_env(inherit=False)
        assert env["PYTHONPATH"] == os.pathsep.join(sys.path)


def test_run_python_module_basic():
    result = run_python_module("sys", capture_output=True, text=True, check=False)
    # python -m sys doesn't exist as runnable, but the command should execute
    # without FileNotFoundError (sys.executable is found)
    assert isinstance(result.returncode, int)


def test_run_python_module_version():
    result = run_python_module("platform", capture_output=True, text=True, check=False)
    # python -m platform prints platform info
    assert result.returncode == 0
    assert result.stdout.strip()  # Should have output


def test_run_python_module_with_pathlike_args(tmp_path: Path):
    """Args accept PathLike objects."""
    result = run_python_module("json.tool", tmp_path / "nonexistent.json", capture_output=True, text=True, check=False)
    # Will fail because file doesn't exist, but shouldn't raise TypeError
    assert result.returncode != 0


def test_generate_shell_wrapper():
    wrapper = generate_shell_wrapper("my.module")
    assert wrapper.startswith("#!/bin/sh\n")
    assert "export PYTHONPATH=" in wrapper
    assert f'exec "{sys.executable}" -m my.module "$@"' in wrapper


def test_generate_shell_wrapper_extra_lines():
    wrapper = generate_shell_wrapper("my.module", extra_lines='export FOO="bar"')
    assert 'export FOO="bar"' in wrapper


def test_generate_shell_wrapper_baked_env():
    wrapper = generate_shell_wrapper("my.module", baked_env={"MY_VAR": "/some/path"})
    assert "export MY_VAR=/some/path" in wrapper
    assert "export PYTHONPATH=" in wrapper


@pytest.mark.parametrize(
    ("env", "expected"),
    [
        ({"FOO": "bar"}, ["export FOO=bar"]),
        ({"BAZ": "/some/path"}, ["export BAZ=/some/path"]),
        ({"P": "/a:/b"}, ["export P=/a:/b"]),  # colon-separated paths (PYTHONPATH-style)
        ({"DIR": Path("/some/path")}, ["export DIR=/some/path"]),  # Path values
        ({"MSG": "hello world"}, ["export MSG='hello world'"]),  # space requires quoting
        ({"EXPR": "a$b"}, ["export EXPR='a$b'"]),  # $ requires quoting
    ],
)
def test_exports_from_dict(env: dict, expected: list[str]):
    assert exports_from_dict(env) == expected


def test_write_shell_wrapper(tmp_path: Path):
    path = tmp_path / "wrapper.sh"
    result = write_shell_wrapper(path, "my.module")
    assert result == path
    assert path.exists()
    assert path.stat().st_mode & 0o755
    content = path.read_text()
    assert content.startswith("#!/bin/sh\n")
    assert "my.module" in content


if __name__ == "__main__":
    pytest_bazel.main()
