"""Test the stop hook fresh scanning functionality."""

import subprocess

import pytest
from hamcrest import all_of, assert_that, contains_string, has_entries

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


def test_stop_hook_fresh_scan_finds_errors(handler, session_id, tmp_path):
    """Test that stop hook runs fresh scans and finds errors."""
    # Create a Python file with violations
    bad_file = tmp_path / "bad_code.py"
    bad_file.write_text("""
try:
    something()
except:  # Bare except
    pass

def check_attr(obj):
    if hasattr(obj, 'foo'):  # hasattr usage
        return getattr(obj, 'foo')  # getattr usage
""")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True, capture_output=True)

    # Create stop hook request
    request = StopRequest(hook_event_name="Stop", session_id=session_id)

    # Handle the hook
    result = handler.handle("Stop", request)

    # Should block due to errors
    response_dict = result.model_dump(by_alias=True)

    # Check response keys and snippets using PyHamcrest (avoid exact long-line match)
    assert_that(
        response_dict,
        has_entries(
            decision="block", reason=all_of(contains_string("Do not use bare `except`"), contains_string(str(bad_file)))
        ),
    )
    assert response_dict["continue"] is True


def test_stop_hook_fresh_scan_passes_clean_code(handler, session_id, tmp_path):
    """Test that stop hook passes when code is clean."""
    # Create a clean Python file
    good_file = tmp_path / "good_code.py"
    good_file.write_text("""
def add(a: int, b: int) -> int:
    return a + b

def safe_divide(a: float, b: float) -> float | None:
    if b == 0:
        return None
    return a / b
""")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True, capture_output=True)

    # Create stop hook request
    request = StopRequest(hook_event_name="Stop", session_id=session_id)

    # Handle the hook
    result = handler.handle("Stop", request)

    # Should pass - check exact response
    response_dict = result.model_dump(by_alias=True)
    assert response_dict == {
        "continue": True,
        "stopReason": None,
        "suppressOutput": None,
        "decision": None,
        "reason": None,
    }


def test_stop_hook_ignores_non_python_files(handler, session_id, tmp_path):
    """Test that stop hook ignores non-Python files."""
    # Create non-Python files
    (tmp_path / "data.json").write_text('{"bad": "except"}')
    (tmp_path / "script.sh").write_text("except: something")
    (tmp_path / "README.md").write_text("# except hasattr")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True, capture_output=True)

    # Create stop hook request
    request = StopRequest(hook_event_name="Stop", session_id=session_id)

    # Handle the hook
    result = handler.handle("Stop", request)

    # Should pass (no Python files) - check exact response
    response_dict = result.model_dump(by_alias=True)
    assert response_dict == {
        "continue": True,
        "stopReason": None,
        "suppressOutput": None,
        "decision": None,
        "reason": None,
    }


def test_stop_hook_quality_gate_disabled(handler, session_id, tmp_path):
    """Test that stop hook passes when quality gate is disabled."""
    # Disable quality gate
    handler.config_loader.config.hooks["stop"].quality_gate = False

    # Create a Python file with violations
    bad_file = tmp_path / "bad_code.py"
    bad_file.write_text("except: pass")  # Invalid syntax but we don't care
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True, capture_output=True)

    # Create stop hook request
    request = StopRequest(hook_event_name="Stop", session_id=session_id)

    # Handle the hook
    result = handler.handle("Stop", request)

    # Should pass despite errors - check exact response
    response_dict = result.model_dump(by_alias=True)
    assert response_dict == {
        "continue": True,
        "stopReason": None,
        "suppressOutput": None,
        "decision": None,
        "reason": None,
    }
