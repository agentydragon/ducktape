"""Tests for CLI-mode environment file generation.

Verifies that write_env_file with CLI-mode EnvVars writes the shims PATH,
SESSION_BAZELRC, and extra_env_script (including direnv) into subsequent Bash
tool calls.
"""

from pathlib import Path

import pytest
import pytest_bazel

from devinfra.claude.env_file import EnvVars, write_env_file


def _cli_env_vars(wrapper_dir: Path, bazelrc: Path, *, extra_env_script: str | None = None) -> EnvVars:
    return EnvVars(shims_dir=wrapper_dir, session_bazelrc=bazelrc, extra_env_script=extra_env_script)


@pytest.fixture
def env_file(tmp_path: Path) -> Path:
    return tmp_path / "env.sh"


@pytest.fixture
def wrapper_dir(tmp_path: Path) -> Path:
    return tmp_path / "bin"


@pytest.fixture
def bazelrc(tmp_path: Path) -> Path:
    path = tmp_path / "bazelrc"
    path.write_text("# test")
    return path


def test_contains_wrapper_path(env_file: Path, wrapper_dir: Path, bazelrc: Path) -> None:
    """Env file puts the wrapper directory on PATH."""
    write_env_file(env_file, _cli_env_vars(wrapper_dir, bazelrc))

    content = env_file.read_text()
    assert str(wrapper_dir) in content
    assert "PATH=" in content


def test_exports_session_bazelrc(env_file: Path, wrapper_dir: Path, bazelrc: Path) -> None:
    """Env file exports SESSION_BAZELRC pointing to the rendered bazelrc."""
    write_env_file(env_file, _cli_env_vars(wrapper_dir, bazelrc))

    content = env_file.read_text()
    assert "SESSION_BAZELRC=" in content
    assert str(bazelrc) in content


def test_extra_env_script_with_direnv(env_file: Path, wrapper_dir: Path, bazelrc: Path) -> None:
    """extra_env_script content (e.g. direnv eval) is included verbatim."""
    direnv_line = 'if command -v direnv >/dev/null 2>&1; then eval "$(direnv export bash 2>/dev/null)"; fi'
    write_env_file(env_file, _cli_env_vars(wrapper_dir, bazelrc, extra_env_script=direnv_line))

    content = env_file.read_text()
    assert "direnv export bash" in content


def test_no_direnv_without_extra_env(env_file: Path, wrapper_dir: Path, bazelrc: Path) -> None:
    """Without extra_env_script, no direnv eval appears."""
    write_env_file(env_file, _cli_env_vars(wrapper_dir, bazelrc))

    content = env_file.read_text()
    assert "direnv export" not in content


if __name__ == "__main__":
    pytest_bazel.main()
