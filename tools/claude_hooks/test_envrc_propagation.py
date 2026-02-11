"""Tests for CLI-mode direnv integration.

Verifies that write_direnv_env_file writes a dynamic eval snippet
(not static exports), so .envrc changes mid-session propagate into
subsequent Bash tool calls.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import pytest_bazel

from tools.claude_hooks.env_file import write_direnv_env_file


def test_write_direnv_env_file_writes_eval_snippet(tmp_path: Path) -> None:
    """Env file contains a dynamic direnv eval, not static exports."""
    env_file = tmp_path / "env.sh"
    write_direnv_env_file(env_file)

    content = env_file.read_text()
    assert 'eval "$(direnv export bash 2>/dev/null)"' in content


def test_write_direnv_env_file_no_static_exports(tmp_path: Path) -> None:
    """Env file should NOT contain static export lines (only the eval)."""
    env_file = tmp_path / "env.sh"
    write_direnv_env_file(env_file)

    content = env_file.read_text()
    # The only "export" should be inside the eval, not standalone export KEY=VALUE lines
    for line in content.splitlines():
        if line.startswith("export "):
            pytest.fail(f"Found static export line: {line!r}")


if __name__ == "__main__":
    pytest_bazel.main()
