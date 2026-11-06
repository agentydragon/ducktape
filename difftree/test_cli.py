"""Tests for CLI functionality."""

from pathlib import Path

import pytest
import pytest_bazel
from click.testing import CliRunner

from difftree.cli import main
from difftree.conftest import create_file, git_add_commit


@pytest.fixture
def runner():
    """Create Click CLI test runner."""
    return CliRunner()


@pytest.fixture
def git_repo_with_changes(temp_git_repo: Path, run_git) -> Path:
    create_file(temp_git_repo, "src/main.py", "line1\n")
    create_file(temp_git_repo, "src/utils.py", "line1\n")
    git_add_commit(run_git)

    create_file(temp_git_repo, "src/main.py", "line1\nline2\nline3\n")
    create_file(temp_git_repo, "README.md", "# Project\n")

    return temp_git_repo


def test_cli_columns_actually_shows_content(runner, git_repo_with_changes, monkeypatch):
    """Test that --columns flag actually controls what appears in output."""
    monkeypatch.chdir(git_repo_with_changes)

    for columns, has_counts in [("tree", False), ("tree,counts", True)]:
        result = runner.invoke(main, ["--columns", columns], obj={}, catch_exceptions=False)
        assert result.exit_code == 0
        assert "main.py" in result.output
        assert ("+2" in result.output) == has_counts


def test_cli_sort_actually_changes_order(runner, temp_git_repo, run_git, monkeypatch):
    """Test that --sort flag actually changes file order."""
    # Create files with different sizes and names
    create_file(temp_git_repo, "a_small.py", "line1\n")
    create_file(temp_git_repo, "z_large.py", "line1\n")
    git_add_commit(run_git)

    # Make z_large.py have more changes
    create_file(temp_git_repo, "a_small.py", "line1\nline2\n")  # +1 line
    create_file(temp_git_repo, "z_large.py", "line1\nline2\nline3\nline4\nline5\n")  # +4 lines

    monkeypatch.chdir(temp_git_repo)

    for sort, first, second in [("size", "z_large", "a_small"), ("alpha", "a_small", "z_large")]:
        result = runner.invoke(main, ["--sort", sort])
        assert result.exit_code == 0
        assert 0 <= result.output.find(first) < result.output.find(second)


def test_cli_columns_flag_invalid_column(runner, git_repo_with_changes):
    """Test --columns flag with invalid column name."""
    result = runner.invoke(main, ["--columns", "tree,invalid,counts"], obj={}, catch_exceptions=False)

    assert result.exit_code == 2
    assert "invalid" in result.output.lower()


def test_cli_no_changes(runner, temp_git_repo, run_git, monkeypatch):
    """Test CLI when there are no changes to display."""
    create_file(temp_git_repo, "file.py", "line1\n")
    git_add_commit(run_git)

    monkeypatch.chdir(temp_git_repo)
    result = runner.invoke(main, [])

    assert result.exit_code == 0
    assert result.stdout == ""  # no tree rendered
    assert result.stderr != ""  # the notice goes to stderr


# CLI Integration Tests


def test_cli_integration_basic(runner, temp_git_repo, run_git, monkeypatch):
    create_file(temp_git_repo, "file.py", "line1\n")
    git_add_commit(run_git)
    create_file(temp_git_repo, "file.py", "line1\nline2\n")

    monkeypatch.chdir(temp_git_repo)
    result = runner.invoke(main, [])

    assert result.exit_code == 0
    assert "file.py" in result.output
    assert "+1" in result.output


def test_cli_integration_with_args(runner, temp_git_repo, run_git, monkeypatch):
    create_file(temp_git_repo, "file.py", "v1\n")
    git_add_commit(run_git)
    create_file(temp_git_repo, "file.py", "v2\n")
    git_add_commit(run_git)

    monkeypatch.chdir(temp_git_repo)
    result = runner.invoke(main, ["HEAD~1", "HEAD"])

    assert result.exit_code == 0
    assert "file.py" in result.output
    assert "+1" in result.output
    assert "-1" in result.output


def test_cli_integration_with_pathspec(runner, temp_git_repo, run_git, monkeypatch):
    """Test CLI with multi-commit diff filtered by pathspec."""
    # Commit 1: Create files in multiple directories
    create_file(temp_git_repo, "src/core/engine.py", "def start():\n    pass\n")
    create_file(temp_git_repo, "src/utils/helpers.py", "def help():\n    pass\n")
    create_file(temp_git_repo, "tests/test_core.py", "def test_start():\n    assert True\n")
    create_file(temp_git_repo, "docs/README.md", "# Documentation\n")
    git_add_commit(run_git)

    # Commit 2: Modify files in different directories
    create_file(temp_git_repo, "src/core/engine.py", "def start():\n    print('starting')\n    pass\n")
    create_file(temp_git_repo, "src/utils/helpers.py", "def help():\n    print('helping')\n    pass\n")
    create_file(
        temp_git_repo, "tests/test_core.py", "def test_start():\n    assert True\n\ndef test_stop():\n    assert True\n"
    )
    create_file(temp_git_repo, "docs/README.md", "# Documentation\n\nDetailed docs here.\n")
    git_add_commit(run_git)

    monkeypatch.chdir(temp_git_repo)

    # Filter to only show src/ changes using pathspec
    result = runner.invoke(main, ["HEAD~1", "HEAD", "--", "src/"])

    assert result.exit_code == 0
    # Should show src/ files
    assert "src" in result.output
    assert "engine.py" in result.output
    assert "helpers.py" in result.output
    # Should NOT show tests/ or docs/ files
    assert "test_core.py" not in result.output
    assert "README.md" not in result.output

    # Verify it shows actual changes (not just file names)
    assert "+1" in result.output  # Added lines in src files


if __name__ == "__main__":
    pytest_bazel.main()
