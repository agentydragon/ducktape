"""Tests for CLI-mode environment file generation.

Verifies that write_env_file with CLI-mode EnvVars writes the wrapper PATH,
SESSION_BAZELRC, and direnv eval for .envrc propagation into subsequent Bash
tool calls.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
import pytest_bazel

from tools.claude_hooks.env_file import EnvVars, write_env_file


def _cli_env_vars(wrapper_dir: Path, bazelrc: Path, *, with_direnv: bool = False) -> EnvVars:
    return EnvVars(bazel_wrapper_dir=wrapper_dir, session_bazelrc=bazelrc, with_direnv=with_direnv)


@pytest.fixture
def cli_env(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Create env file, wrapper dir, and bazelrc paths for CLI env tests."""
    env_file = tmp_path / "env.sh"
    wrapper_dir = tmp_path / "bin"
    bazelrc = tmp_path / "bazelrc"
    bazelrc.write_text("# test")
    return env_file, wrapper_dir, bazelrc


def test_contains_wrapper_path(cli_env: tuple[Path, Path, Path]) -> None:
    """Env file puts the wrapper directory on PATH."""
    env_file, wrapper_dir, bazelrc = cli_env
    write_env_file(env_file, _cli_env_vars(wrapper_dir, bazelrc))

    content = env_file.read_text()
    assert str(wrapper_dir) in content
    assert "PATH=" in content


def test_exports_session_bazelrc(cli_env: tuple[Path, Path, Path]) -> None:
    """Env file exports SESSION_BAZELRC pointing to the rendered bazelrc."""
    env_file, wrapper_dir, bazelrc = cli_env
    write_env_file(env_file, _cli_env_vars(wrapper_dir, bazelrc))

    content = env_file.read_text()
    assert "SESSION_BAZELRC=" in content
    assert str(bazelrc) in content


def test_includes_direnv_eval(cli_env: tuple[Path, Path, Path]) -> None:
    """When direnv is available, env file includes dynamic eval."""
    env_file, wrapper_dir, bazelrc = cli_env
    with patch("tools.claude_hooks.env_file.shutil.which", return_value="/usr/bin/direnv"):
        write_env_file(env_file, _cli_env_vars(wrapper_dir, bazelrc, with_direnv=True))

    content = env_file.read_text()
    assert 'eval "$(direnv export bash 2>/dev/null)"' in content


def test_no_direnv_when_missing(cli_env: tuple[Path, Path, Path]) -> None:
    """When direnv is not installed, env file omits the eval."""
    env_file, wrapper_dir, bazelrc = cli_env
    with patch("tools.claude_hooks.env_file.shutil.which", return_value=None):
        write_env_file(env_file, _cli_env_vars(wrapper_dir, bazelrc, with_direnv=True))

    content = env_file.read_text()
    assert "direnv export" not in content


if __name__ == "__main__":
    pytest_bazel.main()
