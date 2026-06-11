"""Tests for Python ruff linter."""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from ducktape_llm_common.claude_linter_v2.linters.python_ruff import PythonRuffLinter

TEST_FILE = Path("/tmp/test.py")


class TestPythonRuffLinter:
    """Test Python ruff linter functionality."""

    @patch("subprocess.run")
    def test_check_ruff_available(self, mock_run):
        """Test detection of ruff availability."""
        # Test ruff available
        mock_run.return_value = MagicMock(returncode=0, stdout="ruff 0.1.0\n")
        linter = PythonRuffLinter()
        assert linter._ruff_available is True

        # Test ruff not available
        mock_run.side_effect = FileNotFoundError()
        linter = PythonRuffLinter()
        assert linter._ruff_available is False

    @patch("subprocess.run")
    def test_check_code_with_violations(self, mock_run):
        """Test checking code with violations."""
        code = """
try:
    x = 1/0
except:
    pass
"""

        # Mock ruff output
        ruff_output = json.dumps(
            [{"code": "E722", "message": "Do not use bare `except`", "location": {"row": 4, "column": 1}, "fix": None}]
        )

        mock_run.return_value = MagicMock(
            returncode=1,  # ruff returns 1 when violations found
            stdout=ruff_output,
            stderr="",
        )

        linter = PythonRuffLinter()
        linter._ruff_available = True

        violations = linter.check_code(code, TEST_FILE)

        assert len(violations) == 1
        assert violations[0].rule == "ruff:E722"
        assert violations[0].line == 4
        assert violations[0].column == 1
        assert "bare `except`" in violations[0].message
        assert violations[0].fixable is False

    @patch("subprocess.run")
    def test_check_code_clean(self, mock_run):
        """Test checking clean code."""
        code = """
def hello():
    print("Hello, world!")
"""

        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

        linter = PythonRuffLinter()
        linter._ruff_available = True

        violations = linter.check_code(code, TEST_FILE)

        assert len(violations) == 0

    @patch("subprocess.run")
    def test_critical_only_filtering(self, mock_run):
        """Test that critical_only filters violations."""
        # Mock output with both critical and non-critical violations
        ruff_output = json.dumps(
            [
                {
                    "code": "E722",  # Critical
                    "message": "Do not use bare `except`",
                    "location": {"row": 4, "column": 1},
                    "fix": None,
                },
                {
                    "code": "F401",  # Not critical
                    "message": "Module imported but unused",
                    "location": {"row": 1, "column": 1},
                    "fix": {"content": ""},
                },
            ]
        )

        mock_run.return_value = MagicMock(returncode=1, stdout=ruff_output, stderr="")

        linter = PythonRuffLinter()
        linter._ruff_available = True

        violations = linter.check_code("code", TEST_FILE, critical_only=True)

        # Should only return the critical violation
        assert len(violations) == 1
        assert violations[0].rule == "ruff:E722"

    @patch("subprocess.run")
    def test_force_select_rules(self, mock_run):
        """Test that force_select rules are passed to ruff."""
        code = "x = 1"
        force_rules = ["E722", "B009", "S113"]

        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

        linter = PythonRuffLinter(force_select=force_rules)
        linter._ruff_available = True

        linter.check_code(code, TEST_FILE, critical_only=False)

        # Verify the command included force-select rules
        call_args = mock_run.call_args[0][0]
        assert "--select" in call_args
        select_index = call_args.index("--select")
        assert call_args[select_index + 1] == "E722,B009,S113"

    @patch("subprocess.run")
    def test_fixable_violations(self, mock_run):
        """Test detection of fixable violations."""
        ruff_output = json.dumps(
            [
                {
                    "code": "F401",
                    "message": "Module imported but unused",
                    "location": {"row": 1, "column": 1},
                    "fix": {"content": "# Fixed content"},
                }
            ]
        )

        mock_run.return_value = MagicMock(returncode=1, stdout=ruff_output, stderr="")

        linter = PythonRuffLinter()
        linter._ruff_available = True

        violations = linter.check_code("import unused", TEST_FILE, critical_only=False)

        assert len(violations) == 1
        assert violations[0].fixable is True

    @patch("subprocess.run")
    def test_ruff_error_handling(self, mock_run):
        """Test handling of ruff errors."""
        mock_run.return_value = MagicMock(
            returncode=2,  # Error code
            stdout="",
            stderr="Ruff configuration error",
        )

        linter = PythonRuffLinter()
        linter._ruff_available = True

        violations = linter.check_code("code", TEST_FILE)

        # Should return empty list on error
        assert violations == []

    @patch("subprocess.run")
    def test_json_parse_error(self, mock_run):
        """Test handling of invalid JSON from ruff."""
        mock_run.return_value = MagicMock(returncode=1, stdout="Invalid JSON", stderr="")

        linter = PythonRuffLinter()
        linter._ruff_available = True

        violations = linter.check_code("code", TEST_FILE)

        # Should return empty list on parse error
        assert violations == []

    def test_rule_explanations(self):
        """Test rule explanation lookup."""
        linter = PythonRuffLinter()

        # Test known rule
        explanation = linter.get_rule_explanation("E722")
        assert "Bare except" in explanation
        assert "specific exception types" in explanation

        # Test unknown rule
        explanation = linter.get_rule_explanation("UNKNOWN")
        assert "Ruff rule UNKNOWN violation" in explanation

    def test_no_ruff_available(self):
        """Test behavior when ruff is not available."""
        linter = PythonRuffLinter()
        linter._ruff_available = False

        violations = linter.check_code("code", TEST_FILE)

        assert violations == []

    @patch("subprocess.run")
    def test_file_path_passed_to_ruff(self, mock_run):
        """Test that file path is passed to ruff."""
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

        linter = PythonRuffLinter()
        linter._ruff_available = True

        linter.check_code("code", Path("/path/to/file.py"))

        # Verify file path was passed
        call_args = mock_run.call_args[0][0]
        assert "--stdin-filename" in call_args
        filename_index = call_args.index("--stdin-filename")
        assert call_args[filename_index + 1] == "/path/to/file.py"
