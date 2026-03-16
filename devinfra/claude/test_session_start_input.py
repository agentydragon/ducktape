"""Unit tests for SessionStartHookInput parsing."""

import pytest
import pytest_bazel

from devinfra.claude.claude_api.hooks.common import PermissionMode
from devinfra.claude.claude_api.hooks.session_start import SessionStartHookInput


def test_missing_permission_mode_defaults_to_none() -> None:
    """Claude Code Web was observed (2025-01-18) not sending permission_mode
    for SessionStart:resume events, despite documentation claiming it's required.
    """
    result = SessionStartHookInput.model_validate(
        {
            "session_id": "s",
            "cwd": "/tmp",
            "transcript_path": "/tmp/t.json",
            "hook_event_name": "SessionStart",
            "source": "resume",
            "model": "claude-sonnet-4-6",
        }
    )
    assert result.permission_mode is None


def test_explicit_permission_mode() -> None:
    result = SessionStartHookInput.model_validate(
        {
            "session_id": "s",
            "cwd": "/tmp",
            "transcript_path": "/tmp/t.json",
            "hook_event_name": "SessionStart",
            "source": "startup",
            "permission_mode": "plan",
            "model": "claude-sonnet-4-6",
        }
    )
    assert result.permission_mode == PermissionMode.PLAN


@pytest.mark.parametrize("permission_mode", list(PermissionMode))
def test_all_permission_modes(permission_mode: PermissionMode) -> None:
    result = SessionStartHookInput.model_validate(
        {
            "session_id": "s",
            "cwd": "/tmp",
            "transcript_path": "/tmp/t.json",
            "hook_event_name": "SessionStart",
            "source": "startup",
            "permission_mode": permission_mode,
            "model": "claude-sonnet-4-6",
        }
    )
    assert result.permission_mode == permission_mode


def test_missing_model_defaults_to_none() -> None:
    """Claude Code doesn't always send the model field in SessionStart events."""
    result = SessionStartHookInput.model_validate(
        {
            "session_id": "s",
            "cwd": "/tmp",
            "transcript_path": "/tmp/t.json",
            "hook_event_name": "SessionStart",
            "source": "startup",
        }
    )
    assert result.model is None


if __name__ == "__main__":
    pytest_bazel.main()
