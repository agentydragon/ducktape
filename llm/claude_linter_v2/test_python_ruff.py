"""Tests for Python ruff linter."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import pytest_bazel

from llm.claude_linter_v2.linters.python_ruff import PythonRuffLinter

TEST_FILE = Path("/tmp/test.py")

_MOCK_FIND = "llm.claude_linter_v2.linters.python_ruff.find_ruff_binary"


@pytest.fixture
def ruff_linter() -> PythonRuffLinter:
    """PythonRuffLinter using real ruff binary (provided via RUFF_BIN)."""
    return PythonRuffLinter()


def test_ruff_available(ruff_linter):
    assert ruff_linter._ruff_bin is not None


def test_ruff_missing_raises():
    with patch(_MOCK_FIND, return_value=None), pytest.raises(RuntimeError, match="ruff binary not found"):
        PythonRuffLinter()


def test_check_code_with_violations(ruff_linter):
    code = """\
try:
    x = 1 / 0
except:
    pass
"""
    violations = ruff_linter.check_code(code, TEST_FILE)

    assert len(violations) >= 1
    e722 = [v for v in violations if v.rule == "ruff:E722"]
    assert len(e722) == 1
    assert e722[0].line == 3
    assert "bare" in e722[0].message.lower() or "except" in e722[0].message.lower()
    assert e722[0].fixable is False


def test_check_code_clean(ruff_linter):
    code = """\
def hello():
    print("Hello, world!")
"""
    violations = ruff_linter.check_code(code, TEST_FILE)

    assert len(violations) == 0


def test_critical_only_filtering(ruff_linter):
    code = """\
import os

try:
    something()
except:
    pass
"""
    # critical_only=True should find E722 but not F401
    violations = ruff_linter.check_code(code, TEST_FILE, critical_only=True)

    rules = {v.rule for v in violations}
    assert "ruff:E722" in rules
    assert "ruff:F401" not in rules


def test_force_select_rules():
    code = "x = 1\n"
    force_rules = ["E722", "B009", "S113"]

    linter = PythonRuffLinter(force_select=force_rules)
    violations = linter.check_code(code, TEST_FILE, critical_only=False)
    assert violations == []


def test_fixable_violations(ruff_linter):
    code = """\
import os
"""
    violations = ruff_linter.check_code(code, TEST_FILE, critical_only=False)

    f401 = [v for v in violations if v.rule == "ruff:F401"]
    assert len(f401) == 1
    assert f401[0].fixable is True


@patch("subprocess.run")
def test_ruff_error_handling(mock_run, ruff_linter):
    mock_run.return_value = MagicMock(returncode=2, stdout="", stderr="Ruff configuration error")

    violations = ruff_linter.check_code("code", TEST_FILE)

    assert violations == []


@patch("subprocess.run")
def test_json_parse_error(mock_run, ruff_linter):
    mock_run.return_value = MagicMock(returncode=1, stdout="Invalid JSON", stderr="")

    violations = ruff_linter.check_code("code", TEST_FILE)

    assert violations == []


def test_rule_explanations(ruff_linter):
    explanation = ruff_linter.get_rule_explanation("E722")
    assert "Bare except" in explanation
    assert "specific exception types" in explanation

    explanation = ruff_linter.get_rule_explanation("UNKNOWN")
    assert "Ruff rule UNKNOWN violation" in explanation


if __name__ == "__main__":
    pytest_bazel.main()
