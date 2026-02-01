"""Tests for Python code formatter."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import pytest_bazel

from llm.claude_linter_v2.config.models import AutofixCategory
from llm.claude_linter_v2.linters.python_formatter import PythonFormatter

TEST_FILE = Path("/tmp/test.py")

_MOCK_FIND = "llm.claude_linter_v2.linters.python_formatter.find_ruff_binary"


@pytest.fixture
def ruff_formatter() -> PythonFormatter:
    """PythonFormatter configured with ruff (real binary via RUFF_BIN)."""
    return PythonFormatter(["ruff"])


def test_ruff_available():
    formatter = PythonFormatter(["ruff"])
    assert formatter._use_ruff is True
    assert formatter._ruff_bin is not None


def test_ruff_missing_raises():
    with patch(_MOCK_FIND, return_value=None), pytest.raises(RuntimeError, match=r"ruff is configured.*not found"):
        PythonFormatter(["ruff"])


def test_unknown_tool_raises():
    with pytest.raises(RuntimeError, match="Unknown formatting tool"):
        PythonFormatter(["nonexistent"])


def test_non_formatting_tool_skipped():
    formatter = PythonFormatter(["mypy"])
    assert formatter._use_ruff is False


def test_format_with_ruff_success(ruff_formatter):
    input_code = "x=1+2\n"

    result, changes = ruff_formatter.format_code(input_code, file_path=TEST_FILE)

    assert result == "x = 1 + 2\n"
    assert changes == ["Applied ruff formatting"]


def test_no_changes_needed(ruff_formatter):
    code = "x = 1 + 2\n"

    result, changes = ruff_formatter.format_code(code, file_path=TEST_FILE)

    assert result == code
    assert changes == []


def test_fix_imports(ruff_formatter):
    input_code = """\
import os
import sys
import json

def foo():
    return json.dumps({})
"""

    result, changes = ruff_formatter.format_code(input_code, file_path=TEST_FILE, categories=[AutofixCategory.IMPORTS])

    # os and sys should be removed as unused
    assert "import os" not in result
    assert "import sys" not in result
    assert "import json" in result
    assert "Fixed import ordering and removed unused imports" in changes


def test_empty_tools():
    formatter = PythonFormatter([])
    code = "x=1+2"
    result, changes = formatter.format_code(code, file_path=TEST_FILE)

    assert result == code
    assert changes == []


def test_all_categories(ruff_formatter, monkeypatch):
    code = "x=1\n"

    mock_apply = MagicMock(return_value=(code, []))
    mock_fix = MagicMock(return_value=(code, []))
    monkeypatch.setattr(ruff_formatter, "_apply_formatting", mock_apply)
    monkeypatch.setattr(ruff_formatter, "_fix_imports", mock_fix)

    ruff_formatter.format_code(code, file_path=TEST_FILE, categories=[AutofixCategory.ALL])

    mock_apply.assert_called_once()
    mock_fix.assert_called_once()


def test_selective_categories(ruff_formatter, monkeypatch):
    code = "x=1\n"

    mock_apply = MagicMock(return_value=(code, []))
    mock_fix = MagicMock(return_value=(code, []))
    monkeypatch.setattr(ruff_formatter, "_apply_formatting", mock_apply)
    monkeypatch.setattr(ruff_formatter, "_fix_imports", mock_fix)

    # Only formatting
    ruff_formatter.format_code(code, file_path=TEST_FILE, categories=[AutofixCategory.FORMATTING])
    mock_apply.assert_called_once()
    mock_fix.assert_not_called()

    mock_apply.reset_mock()
    mock_fix.reset_mock()

    # Only imports
    ruff_formatter.format_code(code, file_path=TEST_FILE, categories=[AutofixCategory.IMPORTS])
    mock_apply.assert_not_called()
    mock_fix.assert_called_once()


def test_file_path_passed_to_tools(ruff_formatter):
    code = "x = 1\n"
    file_path = Path("/path/to/file.py")

    result, changes = ruff_formatter.format_code(code, file_path=file_path)

    assert result == code
    assert changes == []


if __name__ == "__main__":
    pytest_bazel.main()
