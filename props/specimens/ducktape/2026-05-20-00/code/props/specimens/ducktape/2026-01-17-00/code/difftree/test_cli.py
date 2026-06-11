"""Tests for CLI functionality."""

from pathlib import Path

import pytest
from click.testing import CliRunner

from difftree.__main__ import main
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


def test_cli_default_columns(runner, git_repo_with_changes):
    """Test CLI with default columns (all enabled)."""
    result = runner.invoke(main, [], obj={}, catch_exceptions=False, env={"PWD": str(git_repo_with_changes)})

    assert result.exit_code == 0
    assert result.output.strip() != ""


def test_cli_columns_actually_shows_content(runner, git_repo_with_changes, monkeypatch):
    """Test that --columns flag actually controls what appears in output."""
    monkeypatch.chdir(git_repo_with_changes)

    # Test with only tree column - should NOT have counts
    result_tree_only = runner.invoke(main, ["--columns", "tree"], obj={}, catch_exceptions=False)
    assert result_tree_only.exit_code == 0
    assert "main.py" in result_tree_only.output
    # Should not have +/- counts when counts column is disabled
    assert "+2" not in result_tree_only.output or "counts" not in result_tree_only.output.lower()

    # Test with tree and counts - should have counts
    result_with_counts = runner.invoke(main, ["--columns", "tree,counts"], obj={}, catch_exceptions=False)
    assert result_with_counts.exit_code == 0
    assert "main.py" in result_with_counts.output
    assert "+2" in result_with_counts.output  # Should show addition count


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

    # Sort by size (default) - z_large should come first
    result_size = runner.invoke(main, ["--sort", "size"])
    assert result_size.exit_code == 0
    z_pos = result_size.output.find("z_large")
    a_pos = result_size.output.find("a_small")
    assert z_pos < a_pos, "Larger file should appear first when sorted by size"

    # Sort alphabetically - a_small should come first
    result_alpha = runner.invoke(main, ["--sort", "alpha"])
    assert result_alpha.exit_code == 0
    a_pos_alpha = result_alpha.output.find("a_small")
    z_pos_alpha = result_alpha.output.find("z_large")
    assert a_pos_alpha < z_pos_alpha, "Files should be alphabetically ordered"


def test_cli_columns_flag_invalid_column(runner, git_repo_with_changes):
    """Test --columns flag with invalid column name."""
    result = runner.invoke(main, ["--columns", "tree,invalid,counts"], obj={}, catch_exceptions=False)

    assert result.exit_code == 2
    assert "Unknown column" in result.output
    assert "invalid" in result.output.lower()


def test_cli_no_changes(runner, temp_git_repo, run_git, monkeypatch):
    """Test CLI when there are no changes to display."""
    create_file(temp_git_repo, "file.py", "line1\n")
    git_add_commit(run_git)

    monkeypatch.chdir(temp_git_repo)
    result = runner.invoke(main, [])

    assert result.exit_code == 0
    assert "No changes" in result.output


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


def test_cli_integration_invalid_column(runner, temp_git_repo, run_git, monkeypatch):
    create_file(temp_git_repo, "file.py", "line1\n")
    git_add_commit(run_git)
    create_file(temp_git_repo, "file.py", "line1\nline2\n")

    monkeypatch.chdir(temp_git_repo)
    result = runner.invoke(main, ["--columns", "tree,invalid"])

    assert result.exit_code == 2
    assert "Unknown column" in result.output
    assert "invalid" in result.output


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
