"""Test the stop hook quality gate functionality."""

import subprocess

import pytest

from ducktape_llm_common.claude_code_api import StopRequest
from ducktape_llm_common.claude_linter_v2.config.models import StopHookConfig
from ducktape_llm_common.claude_linter_v2.hooks.handler import HookHandler


@pytest.fixture
def handler(tmp_path, monkeypatch):
    """Create a hook handler instance with isolated cwd."""
    monkeypatch.chdir(tmp_path)
    # Initialize git repo so files are tracked
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True, capture_output=True)
    handler = HookHandler()
    # Ensure quality gate is enabled for testing
    stop_config = handler.config_loader.config.hooks["stop"]
    assert isinstance(stop_config, StopHookConfig)
    stop_config.quality_gate = True
    return handler


def test_stop_hook_blocks_with_unfixed_errors(handler, session_id, tmp_path):
    """Test that stop hook blocks when there are unfixed errors."""
    # Create files with violations
    file_py = tmp_path / "file.py"
    file_py.write_text("""
try:
    something()
except:  # Line 4: bare except
    pass

def check(obj):
    if hasattr(obj, 'foo'):  # Line 8: hasattr
        pass
""")
    other_py = tmp_path / "other.py"
    other_py.write_text("x = 1\n")

    # Add files to git
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True, capture_output=True)

    # Create stop hook request
    request = StopRequest(hook_event_name="Stop", session_id=session_id)

    # Handle the hook
    result = handler.handle("Stop", request)

    # Should block due to errors
    response_dict = result.model_dump()
    assert response_dict.get("continue_") is True  # Always True for hook responses
    assert response_dict.get("decision") == "block"  # StopPrevent sets decision=block
    assert response_dict.get("reason") is not None
    reason = response_dict["reason"]
    assert "errors that must be fixed" in reason
    assert "file.py" in reason
    assert "cl2 check" in reason  # Should include check command


def test_stop_hook_allows_with_clean_code(handler, session_id, tmp_path):
    """Test that stop hook allows proceeding with clean code."""
    # Create clean file
    file_py = tmp_path / "file.py"
    file_py.write_text("""
def add(a: int, b: int) -> int:
    return a + b
""")

    # Add to git
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True, capture_output=True)

    # Create stop hook request
    request = StopRequest(hook_event_name="Stop", session_id=session_id)

    # Handle the hook
    result = handler.handle("Stop", request)

    # Should allow stop (no errors)
    response_dict = result.model_dump()
    assert response_dict.get("continue_") is True
    assert response_dict.get("decision") is None


def test_stop_hook_passes_with_no_files(handler, session_id, tmp_path):
    """Test that stop hook passes when there are no Python files."""
    # Create stop hook request
    request = StopRequest(hook_event_name="Stop", session_id=session_id)

    # Handle the hook
    result = handler.handle("Stop", request)

    # Should pass
    response_dict = result.model_dump()
    assert response_dict.get("continue_") is True
    assert response_dict.get("decision") is None


def test_stop_hook_passes_when_quality_gate_disabled(handler, session_id, tmp_path):
    """Test that stop hook passes when quality gate is disabled."""
    # Disable quality gate
    handler.config_loader.config.hooks["stop"].quality_gate = False

    # Create file with violations
    file_py = tmp_path / "file.py"
    file_py.write_text("""
try:
    x = 1
except:
    pass
""")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True, capture_output=True)

    # Create stop hook request
    request = StopRequest(hook_event_name="Stop", session_id=session_id)

    # Handle the hook
    result = handler.handle("Stop", request)

    # Should pass despite errors
    response_dict = result.model_dump()
    assert response_dict.get("continue_") is True
    assert response_dict.get("decision") is None


def test_violation_tracker_deduplication(handler, session_id, tmp_path):
    """Test that duplicate violations are deduplicated in the tracker."""
    file_py = tmp_path / "file.py"

    # Add same violation multiple times
    for _ in range(3):
        handler.violation_tracker.add_violation(
            session_id=session_id,
            file_path=file_py,
            line=10,
            message="Bare except clause not allowed",
            severity="error",
            rule="bare-except",
        )

    # Should only have one violation
    unfixed = handler.violation_tracker.get_unfixed_violations(session_id)
    assert len(unfixed) == 1
