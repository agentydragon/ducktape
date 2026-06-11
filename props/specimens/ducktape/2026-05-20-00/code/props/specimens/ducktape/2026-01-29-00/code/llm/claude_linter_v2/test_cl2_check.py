"""Test the cl2 check command."""

import json

import pytest
import pytest_bazel
from click.testing import CliRunner

from llm.claude_linter_v2.cli import cli


@pytest.fixture
def runner():
    """Create a CLI runner."""
    return CliRunner()


def test_check_single_file(runner, tmp_path):
    """Test checking a single file."""
    # Create a test file with violations
    test_file = tmp_path / "test.py"
    test_file.write_text("""
try:
    x = 1
except:  # bare except
    pass

if hasattr(obj, 'attr'):
    value = getattr(obj, 'attr')
""")

    # Run check
    result = runner.invoke(cli, ["check", str(test_file)])

    # Should find violations
    assert result.exit_code == 1
    assert "test.py:" in result.output
    assert "bare except" in result.output.lower()
    assert "hasattr" in result.output.lower()


def test_check_directory(runner, tmp_path):
    """Test checking a directory."""
    # Create test files
    (tmp_path / "good.py").write_text("x = 1\n")
    (tmp_path / "bad.py").write_text("try:\n    x = 1\nexcept:\n    pass\n")

    # Run check
    result = runner.invoke(cli, ["check", str(tmp_path)])

    # Should find violations only in bad.py
    assert result.exit_code == 1
    assert "bad.py:" in result.output
    assert "good.py:" not in result.output


def test_check_with_fix(runner, tmp_path):
    """Test checking with --fix option."""
    # Create a test file with fixable issues
    test_file = tmp_path / "test.py"
    test_file.write_text("x=1\ny=2\n")  # Missing spaces around =

    # Run check with fix
    result = runner.invoke(cli, ["check", str(test_file), "--fix"])

    # Should fix the file
    assert result.exit_code == 0
    fixed_content = test_file.read_text()
    assert fixed_content == "x = 1\ny = 2\n"


def test_check_json_output(runner, tmp_path):
    """Test JSON output format."""
    # Create a test file
    test_file = tmp_path / "test.py"
    test_file.write_text("try:\n    x = 1\nexcept:\n    pass\n")

    # Run check with JSON output
    result = runner.invoke(cli, ["check", str(test_file), "--json"])

    # Should produce valid JSON
    assert result.exit_code == 1
    data = json.loads(result.output)
    assert data["total_violations"] > 0
    assert data["files_checked"] == 1
    assert str(test_file) in data["results"]


def test_check_no_files_found(runner, tmp_path):
    """Test when no files are found."""
    result = runner.invoke(cli, ["check", str(tmp_path / "nonexistent.py")])

    # Should exit cleanly (no violations = success)
    assert result.exit_code == 0
    # Output indicates no issues (since nonexistent file is skipped with error log)
    assert "No issues found" in result.output


def test_check_clean_files(runner, tmp_path):
    """Test checking clean files."""
    # Create a clean file
    test_file = tmp_path / "clean.py"
    test_file.write_text("x = 1\ny = 2\n")

    # Run check
    result = runner.invoke(cli, ["check", str(test_file)])

    # Should pass
    assert result.exit_code == 0
    assert "No issues found" in result.output


def test_check_categories(runner, tmp_path):
    """Test checking specific categories."""
    # Create a test file
    test_file = tmp_path / "test.py"
    test_file.write_text("x=1\ny=2\n")

    # Run check with specific category
    result = runner.invoke(cli, ["check", str(test_file), "--fix", "--categories", "formatting"])

    # Should fix formatting issues
    assert result.exit_code == 0
    fixed_content = test_file.read_text()
    assert fixed_content == "x = 1\ny = 2\n"


def test_check_invalid_category(runner):
    """Test with invalid category."""
    result = runner.invoke(cli, ["check", "--categories", "invalid_category"])

    # Should show error
    assert result.exit_code == 1
    assert "Unknown category: invalid_category" in result.output
    assert "Valid categories:" in result.output


if __name__ == "__main__":
    pytest_bazel.main()
