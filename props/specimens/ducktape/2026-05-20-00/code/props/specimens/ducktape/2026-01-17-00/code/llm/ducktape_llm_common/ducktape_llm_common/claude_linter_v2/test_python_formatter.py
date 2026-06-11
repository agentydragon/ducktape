"""Tests for Python code formatter."""

from pathlib import Path
from unittest.mock import MagicMock, patch

from ducktape_llm_common.claude_linter_v2.config.models import AutofixCategory
from ducktape_llm_common.claude_linter_v2.linters.python_formatter import PythonFormatter

TEST_FILE = Path("/test.py")


class TestPythonFormatter:
    """Test Python code formatting functionality."""

    @patch("subprocess.run")
    def test_check_available_tools(self, mock_run):
        """Test detection of available formatting tools."""

        # Mock ruff available, black not available
        def side_effect(cmd, **kwargs):
            if cmd[0] == "ruff":
                return MagicMock(returncode=0, stdout="ruff 0.1.0\n")
            raise FileNotFoundError

        mock_run.side_effect = side_effect

        formatter = PythonFormatter(["ruff", "black", "isort"])
        assert formatter._available_tools == ["ruff"]

    @patch("subprocess.run")
    def test_format_with_ruff_success(self, mock_run):
        """Test successful formatting with ruff."""
        input_code = "x=1+2"
        formatted_code = "x = 1 + 2\n"

        mock_run.return_value = MagicMock(returncode=0, stdout=formatted_code, stderr="")

        formatter = PythonFormatter(["ruff"])
        formatter._available_tools = ["ruff"]

        result, changes = formatter.format_code(input_code, file_path=TEST_FILE)

        assert result == formatted_code
        assert changes == ["Applied ruff formatting"]

        # Verify ruff was called correctly
        mock_run.assert_called_with(
            ["ruff", "format", "--stdin-filename", TEST_FILE, "-"],
            input=input_code,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )

    @patch("subprocess.run")
    def test_format_with_black_success(self, mock_run):
        """Test successful formatting with black."""
        input_code = "x=1+2"
        formatted_code = "x = 1 + 2\n"

        mock_run.return_value = MagicMock(returncode=0, stdout=formatted_code, stderr="")

        formatter = PythonFormatter(["black"])
        formatter._available_tools = ["black"]

        result, changes = formatter.format_code(input_code, file_path=TEST_FILE)

        assert result == formatted_code
        assert changes == ["Applied black formatting"]

        # Verify black was called correctly
        mock_run.assert_called_with(
            ["black", "-", "--quiet"], input=input_code, capture_output=True, text=True, timeout=30, check=False
        )

    @patch("subprocess.run")
    def test_no_changes_needed(self, mock_run):
        """Test when code is already formatted."""
        code = "x = 1 + 2\n"

        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=code,  # Same as input
            stderr="",
        )

        formatter = PythonFormatter(["ruff"])
        formatter._available_tools = ["ruff"]

        result, changes = formatter.format_code(code, file_path=TEST_FILE)

        assert result == code
        assert changes == []

    @patch("subprocess.run")
    def test_formatting_error(self, mock_run):
        """Test handling of formatting errors."""
        code = "invalid syntax @#$"

        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="Syntax error")

        formatter = PythonFormatter(["ruff"])
        formatter._available_tools = ["ruff"]

        result, changes = formatter.format_code(code, file_path=TEST_FILE)

        # Should return original code on error
        assert result == code
        assert changes == []

    @patch("subprocess.run")
    def test_fix_imports(self, mock_run):
        """Test import fixing with ruff."""
        input_code = """import os
import sys
from typing import List
import json

def foo():
    return json.dumps({})
"""

        fixed_code = """import json

def foo():
    return json.dumps({})
"""

        # First call for formatting, second for import fixing
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout=input_code),  # No formatting changes
            MagicMock(returncode=1, stdout=fixed_code),  # Fixed imports
        ]

        formatter = PythonFormatter(["ruff"])
        formatter._available_tools = ["ruff"]

        result, changes = formatter.format_code(input_code, file_path=TEST_FILE, categories=[AutofixCategory.IMPORTS])

        assert result == fixed_code
        assert "Fixed import ordering and removed unused imports" in changes

    def test_no_tools_available(self):
        """Test behavior when no tools are available."""
        formatter = PythonFormatter(["nonexistent"])
        formatter._available_tools = []

        code = "x=1+2"
        result, changes = formatter.format_code(code, file_path=TEST_FILE)

        assert result == code
        assert changes == []

    @patch("subprocess.run")
    def test_all_categories(self, mock_run, monkeypatch):
        """Test that ALL category expands to all categories."""
        code = "x=1"

        mock_run.return_value = MagicMock(returncode=0, stdout=code, stderr="")

        formatter = PythonFormatter(["ruff"])
        formatter._available_tools = ["ruff"]

        mock_apply = MagicMock(return_value=(code, []))
        mock_fix = MagicMock(return_value=(code, []))
        monkeypatch.setattr(formatter, "_apply_formatting", mock_apply)
        monkeypatch.setattr(formatter, "_fix_imports", mock_fix)

        formatter.format_code(code, file_path=TEST_FILE, categories=[AutofixCategory.ALL])

        # Both methods should be called
        mock_apply.assert_called_once()
        mock_fix.assert_called_once()

    @patch("subprocess.run")
    def test_selective_categories(self, mock_run, monkeypatch):
        """Test selective category application."""
        code = "x=1"

        formatter = PythonFormatter(["ruff"])
        formatter._available_tools = ["ruff"]

        mock_apply = MagicMock(return_value=(code, []))
        mock_fix = MagicMock(return_value=(code, []))
        monkeypatch.setattr(formatter, "_apply_formatting", mock_apply)
        monkeypatch.setattr(formatter, "_fix_imports", mock_fix)

        # Only formatting
        formatter.format_code(code, file_path=TEST_FILE, categories=[AutofixCategory.FORMATTING])
        mock_apply.assert_called_once()
        mock_fix.assert_not_called()

        # Reset mocks
        mock_apply.reset_mock()
        mock_fix.reset_mock()

        # Only imports
        formatter.format_code(code, file_path=TEST_FILE, categories=[AutofixCategory.IMPORTS])
        mock_apply.assert_not_called()
        mock_fix.assert_called_once()

    @patch("subprocess.run")
    def test_file_path_passed_to_tools(self, mock_run):
        """Test that file path is properly passed to formatting tools."""
        code = "x=1"
        file_path = Path("/path/to/file.py")

        mock_run.return_value = MagicMock(returncode=0, stdout=code, stderr="")

        formatter = PythonFormatter(["ruff"])
        formatter._available_tools = ["ruff"]

        formatter.format_code(code, file_path=file_path)

        # Verify file path was passed to ruff
        mock_run.assert_called_with(
            ["ruff", "format", "--stdin-filename", file_path, "-"],
            input=code,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
