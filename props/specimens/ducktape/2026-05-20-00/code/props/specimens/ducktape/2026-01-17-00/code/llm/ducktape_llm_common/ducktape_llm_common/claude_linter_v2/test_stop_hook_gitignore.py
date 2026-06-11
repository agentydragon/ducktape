"""Test that stop hook respects gitignore."""

import subprocess

from ducktape_llm_common.claude_code_api import StopRequest
from ducktape_llm_common.claude_linter_v2.config.models import StopHookConfig
from ducktape_llm_common.claude_linter_v2.hooks.handler import HookHandler


def _enable_quality_gate(handler: HookHandler) -> None:
    """Helper to enable quality gate with type narrowing."""
    stop_config = handler.config_loader.config.hooks["stop"]
    assert isinstance(stop_config, StopHookConfig)
    stop_config.quality_gate = True


def test_stop_hook_respects_gitignore(session_id, tmp_path, monkeypatch):
    """Test that stop hook respects gitignore and doesn't scan node_modules."""
    monkeypatch.chdir(tmp_path)

    # Initialize git repo
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=tmp_path, check=True, capture_output=True)

    handler = HookHandler()
    _enable_quality_gate(handler)

    # Create .gitignore
    gitignore = tmp_path / ".gitignore"
    gitignore.write_text("node_modules/\nvenv/\n__pycache__/\n*.pyc\n")

    # Create a tracked Python file with violations
    tracked_file = tmp_path / "app.py"
    tracked_file.write_text(
        """
try:
    something()
except:  # Bare except
    pass
"""
    )

    # Create an ignored Python file with violations
    node_modules = tmp_path / "node_modules" / "some_package"
    node_modules.mkdir(parents=True)
    ignored_file = node_modules / "bad_code.py"
    ignored_file.write_text(
        """
try:
    something()
except:  # Many bare excepts
    pass

try:
    other()
except:
    pass
"""
    )

    # Create another ignored file in venv
    venv = tmp_path / "venv" / "lib"
    venv.mkdir(parents=True)
    venv_file = venv / "library.py"
    venv_file.write_text(
        """
def bad():
    try:
        x = 1
    except:
        pass
"""
    )

    # Add and commit the tracked file (not the ignored ones)
    subprocess.run(["git", "add", ".gitignore", "app.py"], check=True)
    subprocess.run(["git", "commit", "-m", "Initial commit"], check=True)

    # Create stop hook request
    request = StopRequest(hook_event_name="Stop", session_id=session_id)

    # Handle the hook
    result = handler.handle("Stop", request)

    # Should block due to errors in tracked file only
    response_dict = result.model_dump(by_alias=True)

    # Check main keys and verify reason contains tracked filename and violation snippets
    assert response_dict["continue"] is True
    assert response_dict["decision"] == "block"
    assert str(tracked_file) in response_dict["reason"]
    assert "Do not use bare `except`" in response_dict["reason"]


def test_stop_hook_fallback_when_not_git_repo(session_id, tmp_path, monkeypatch):
    """Test that stop hook falls back to all files when not in a git repo."""
    monkeypatch.chdir(tmp_path)

    handler = HookHandler()
    _enable_quality_gate(handler)

    # Don't initialize git - just create files

    # Create a Python file with violations
    bad_file = tmp_path / "bad_code.py"
    bad_file.write_text(
        """
try:
    something()
except:  # Bare except
    pass
"""
    )

    # Create node_modules (would be ignored if git was present)
    node_modules = tmp_path / "node_modules"
    node_modules.mkdir()
    node_file = node_modules / "package.py"
    node_file.write_text(
        """
try:
    x = 1
except:
    pass
"""
    )

    # Create stop hook request
    request = StopRequest(hook_event_name="Stop", session_id=session_id)

    # Handle the hook
    result = handler.handle("Stop", request)

    # Should find violations in all files (no git = no gitignore)
    response_dict = result.model_dump(by_alias=True)

    # Both files have bare-except violations
    assert response_dict["continue"] is True
    assert response_dict["decision"] == "block"
    assert str(bad_file) in response_dict["reason"]
    assert str(node_file) in response_dict["reason"]
    assert "Do not use bare `except`" in response_dict["reason"]
