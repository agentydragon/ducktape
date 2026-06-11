"""Integration tests for claude-linter-v2."""

import json
import subprocess
import sys

import pytest


def _has_ruff() -> bool:
    """Check if ruff CLI is available."""
    try:
        # Use python -m ruff to work in Bazel sandbox
        result = subprocess.run(
            [sys.executable, "-m", "ruff", "--version"], capture_output=True, check=False, timeout=5
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


class TestCLIIntegration:
    """Test the full CLI integration."""

    def test_pre_hook_bare_except(self, tmp_path):
        """Test that pre-hook blocks bare except."""
        request_data = {
            "hook_event_name": "PreToolUse",
            "tool_name": "Write",
            "tool_input": {
                "file_path": str(tmp_path / "test_bare_except.py"),
                "content": """
try:
    x = 1/0
except:
    pass
""",
            },
            "session_id": "12345678-1234-5678-1234-567812345678",
        }

        result = subprocess.run(
            [sys.executable, "-m", "ducktape_llm_common.claude_linter_v2.cli", "hook"],
            input=json.dumps(request_data),
            capture_output=True,
            text=True,
            check=False,
        )

        assert result.returncode == 0  # CLI always exits 0, check JSON response
        response = json.loads(result.stdout)
        assert response["decision"] == "block"
        assert "bare except" in response["reason"].lower()
        assert "Line 4:" in response["reason"]

    def test_pre_hook_hasattr(self, tmp_path):
        """Test that pre-hook blocks hasattr usage."""
        request_data = {
            "hook_event_name": "PreToolUse",
            "tool_name": "Write",
            "tool_input": {
                "file_path": str(tmp_path / "test_hasattr.py"),
                "content": """
obj = object()
if hasattr(obj, 'foo'):
    print("has foo")
""",
            },
            "session_id": "12345678-1234-5678-1234-567812345679",
        }

        result = subprocess.run(
            [sys.executable, "-m", "ducktape_llm_common.claude_linter_v2.cli", "hook"],
            input=json.dumps(request_data),
            capture_output=True,
            text=True,
            check=False,
        )

        assert result.returncode == 0
        response = json.loads(result.stdout)
        assert response["decision"] == "block"
        assert "hasattr" in response["reason"]
        assert "Line 3:" in response["reason"]

    def test_pre_hook_clean_code(self, tmp_path):
        """Test that pre-hook passes clean code."""
        request_data = {
            "hook_event_name": "PreToolUse",
            "tool_name": "Write",
            "tool_input": {
                "file_path": str(tmp_path / "test_clean.py"),
                "content": """
def hello():
    try:
        print("Hello, world!")
    except ValueError as e:
        print(f"Error: {e}")
""",
            },
            "session_id": "12345678-1234-5678-1234-567812345680",
        }

        result = subprocess.run(
            [sys.executable, "-m", "ducktape_llm_common.claude_linter_v2.cli", "hook"],
            input=json.dumps(request_data),
            capture_output=True,
            text=True,
            check=False,
        )

        assert result.returncode == 0
        response = json.loads(result.stdout)
        assert response["continue"] is True
        # Clean code should not be blocked
        assert response.get("decision") != "block"

    @pytest.mark.skipif(not _has_ruff(), reason="ruff not available")
    def test_pre_hook_ruff_violation(self, tmp_path):
        """Test that pre-hook blocks ruff violations."""
        request_data = {
            "hook_event_name": "PreToolUse",
            "tool_name": "Write",
            "tool_input": {
                "file_path": str(tmp_path / "test_mutable_default.py"),
                "content": """
import os

def get_data():
    # Mutable default argument
    def process(items=[]):
        items.append(1)
        return items
""",
            },
            "session_id": "12345678-1234-5678-1234-567812345681",
        }

        result = subprocess.run(
            [sys.executable, "-m", "ducktape_llm_common.claude_linter_v2.cli", "hook"],
            input=json.dumps(request_data),
            capture_output=True,
            text=True,
            check=False,
        )

        assert result.returncode == 0
        response = json.loads(result.stdout)
        assert response["decision"] == "block"
        assert "mutable" in response["reason"].lower()
        assert "Line 6:" in response["reason"]

    def test_pre_hook_barrel_init(self, tmp_path):
        """Test that pre-hook blocks barrel __init__.py."""
        request_data = {
            "hook_event_name": "PreToolUse",
            "tool_name": "Write",
            "tool_input": {
                "file_path": str(tmp_path / "__init__.py"),
                "content": """
from .module1 import *
from .module2 import Class1, Class2

__all__ = ['Class1', 'Class2']
""",
            },
            "session_id": "12345678-1234-5678-1234-567812345682",
        }

        result = subprocess.run(
            [sys.executable, "-m", "ducktape_llm_common.claude_linter_v2.cli", "hook"],
            input=json.dumps(request_data),
            capture_output=True,
            text=True,
            check=False,
        )

        assert result.returncode == 0
        response = json.loads(result.stdout)
        assert response["decision"] == "block"
        assert "barrel" in response["reason"].lower()

    def test_pre_hook_invalid_json(self):
        """Test that pre-hook handles invalid JSON gracefully."""
        result = subprocess.run(
            [sys.executable, "-m", "ducktape_llm_common.claude_linter_v2.cli", "hook"],
            input="not valid json",
            capture_output=True,
            text=True,
            check=False,
        )

        # Invalid JSON should crash the CLI
        assert result.returncode != 0
        assert "JSON parse error" in result.stderr

    def test_pre_hook_non_python_file(self, tmp_path):
        """Test that pre-hook passes non-Python files."""
        request_data = {
            "hook_event_name": "PreToolUse",
            "tool_name": "Write",
            "tool_input": {
                "file_path": str(tmp_path / "test.txt"),
                "content": "This is just a text file with except: and hasattr",
            },
            "session_id": "12345678-1234-5678-1234-567812345683",
        }

        result = subprocess.run(
            [sys.executable, "-m", "ducktape_llm_common.claude_linter_v2.cli", "hook"],
            input=json.dumps(request_data),
            capture_output=True,
            text=True,
            check=False,
        )

        assert result.returncode == 0
        response = json.loads(result.stdout)
        assert response["continue"] is True
        # Non-Python files just return {"continue": true}

    def test_post_hook_basic(self, tmp_path):
        """Test that post-hook runs without errors."""
        request_data = {
            "hook_event_name": "PostToolUse",
            "tool_name": "Write",
            "tool_input": {"file_path": str(tmp_path / "test_post.py"), "content": "x=1+2  # poorly formatted"},
            "session_id": "12345678-1234-5678-1234-567812345684",
        }

        result = subprocess.run(
            [sys.executable, "-m", "ducktape_llm_common.claude_linter_v2.cli", "hook"],
            input=json.dumps(request_data),
            capture_output=True,
            text=True,
            check=False,
        )

        # CLI always exits 0
        assert result.returncode == 0
        response = json.loads(result.stdout)
        assert response["continue"] is True
        # Post-hook may apply autofix (ruff formatting) even to clean code
        if response.get("decision") == "block":
            assert "Autofix:" in response["reason"]


class TestSessionCommands:
    """Test session management commands."""

    def test_session_list(self, tmp_path):
        """Test listing sessions."""
        result = subprocess.run(
            [sys.executable, "-m", "ducktape_llm_common.claude_linter_v2.cli", "session", "list"],
            capture_output=True,
            text=True,
            cwd=tmp_path,
            check=False,
        )

        assert result.returncode == 0
        # Isolated env has no sessions
        assert "No active sessions found" in result.stdout

    def test_session_allow(self, tmp_path):
        """Test adding an allow rule."""
        result = subprocess.run(
            [sys.executable, "-m", "ducktape_llm_common.claude_linter_v2.cli", "session", "allow", "Edit('**/*.py')"],
            capture_output=True,
            text=True,
            cwd=tmp_path,
            check=False,
        )

        assert result.returncode == 0
        # Isolated env has no sessions to apply rule to
        assert "No active sessions found" in result.stdout
