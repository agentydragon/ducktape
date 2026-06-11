"""Shared fixtures for claude_linter_v2 tests."""

from pathlib import Path
from typing import Any

import pytest

from llm.claude_code_api import PostToolUseRequest, PreToolUseRequest
from llm.claude_linter_v2.types import SessionID, parse_session_id

# Synthetic file path for tests that need a path but don't create real files
TEST_FILE = Path("/test/file.py")


def make_pre_tool_request(session_id: SessionID, tool_name: str, tool_input: dict[str, Any]) -> PreToolUseRequest:
    """Create a PreToolUseRequest using model_validate to trigger validators."""
    return PreToolUseRequest.model_validate(
        {"session_id": session_id, "hook_event_name": "PreToolUse", "tool_name": tool_name, "tool_input": tool_input}
    )


def make_post_tool_request(
    session_id: SessionID, tool_name: str, tool_input: dict[str, Any], tool_result: dict[str, Any] | None = None
) -> PostToolUseRequest:
    """Create a PostToolUseRequest using model_validate to trigger validators."""
    return PostToolUseRequest.model_validate(
        {
            "session_id": session_id,
            "hook_event_name": "PostToolUse",
            "tool_name": tool_name,
            "tool_input": tool_input,
            "tool_result": tool_result,
        }
    )


@pytest.fixture(autouse=True)
def _isolate_data_dir(tmp_path, monkeypatch):
    """Isolate tests from real user data and each other."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))


@pytest.fixture
def session_id():
    """Create a test session ID using all-zeros UUID."""
    return parse_session_id("00000000-0000-0000-0000-000000000000")
