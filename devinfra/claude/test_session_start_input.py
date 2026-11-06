"""Unit tests for SessionStartHookInput parsing."""

import pytest
import pytest_bazel

from devinfra.claude.claude_api.hooks.common import PermissionMode
from devinfra.claude.claude_api.hooks.session_start import SessionStartHookInput


@pytest.mark.parametrize(
    ("event_fields", "expected_model"),
    [({"source": "resume", "model": "claude-sonnet-5"}, "claude-sonnet-5"), ({"source": "startup"}, None)],
    ids=["resume-omits-permission-mode", "startup-omits-model"],
)
def test_optional_session_start_fields(event_fields: dict[str, str], expected_model: str | None) -> None:
    """Web omitted permission_mode on resume (2025-01-18); some events omit model."""
    result = SessionStartHookInput.model_validate(
        {
            "session_id": "s",
            "cwd": "/tmp",
            "transcript_path": "/tmp/t.json",
            "hook_event_name": "SessionStart",
            **event_fields,
        }
    )
    assert result.model == expected_model
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
            "model": "claude-sonnet-5",
        }
    )
    assert result.permission_mode == PermissionMode.PLAN


if __name__ == "__main__":
    pytest_bazel.main()
