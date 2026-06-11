"""Unit tests for MCP tool handling in claude-linter-v2."""

import json
from datetime import datetime
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import pytest_bazel

from llm.claude_code_api import PostToolUseRequest, PreToolUseRequest
from llm.claude_linter_v2.config.clean_models import ModularConfig
from llm.claude_linter_v2.config.models import AutofixCategory, PostToolHookConfig
from llm.claude_linter_v2.hooks.handler import HookHandler
from llm.claude_linter_v2.types import SessionID
from llm.claude_outcomes import PostToolSuccess, PreToolApprove


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


class TestMCPTools:
    """Test MCP tool handling in hooks."""

    @pytest.fixture
    def handler(self):
        """Create a handler instance."""
        return HookHandler()

    def test_mcp_tool_with_extra_fields(self, handler, session_id):
        """Test that MCP tools with custom fields are handled correctly."""
        # Create a request with MCP-specific fields
        request = make_pre_tool_request(
            session_id=session_id,
            tool_name="mcp_memory_search_nodes",
            tool_input={
                # Standard fields
                "file_path": None,
                "content": None,
                # MCP-specific fields (should be allowed by extra="allow")
                "query": "authentication patterns",
                "limit": 5,
                "server": "memory",
            },
        )

        # Should approve since it's not a file operation
        outcome = handler._handle_pre_hook(request, session_id)
        assert isinstance(outcome, PreToolApprove)

    def test_mcp_filesystem_tool_with_path(self, handler, session_id, monkeypatch):
        """Test MCP filesystem tool that should trigger path checks."""
        # Create a minimal mock config that properly mocks the nested structure
        mock_config = MagicMock()
        mock_config.access_control = []
        mock_config.repo_rules = []
        mock_config.hooks = {"post": MagicMock(auto_fix=False, inject_permissions=False)}
        mock_config.python.hard_blocks.bare_except = True
        mock_config.python.hard_blocks.getattr_setattr = True
        mock_config.python.hard_blocks.barrel_init = True
        mock_config.python.ruff_force_select = []
        mock_config.max_errors_to_show = 3

        # Mock the config at instance level (restored automatically by monkeypatch)
        monkeypatch.setattr(handler.config_loader, "_config", mock_config)

        request = make_pre_tool_request(
            session_id=session_id,
            tool_name="mcp_filesystem_write_file",
            tool_input={"path": "/tmp/test.txt", "content": "Hello, world!"},
        )

        # Should check access control even for MCP tools
        outcome = handler._handle_pre_hook(request, session_id)
        assert isinstance(outcome, PreToolApprove)

    def test_mcp_tool_with_python_file(self, handler, session_id):
        """Test MCP tool operating on Python files - MCP tools are NOT checked for Python violations."""
        request = make_pre_tool_request(
            session_id=session_id,
            tool_name="mcp_editor_open",
            tool_input={
                "file_path": Path("/home/user/test.py"),
                "content": "try:\n    pass\nexcept:\n    pass",  # Bare except
            },
        )

        # MCP tools are not checked for Python violations - only known tool types are
        outcome = handler._handle_pre_hook(request, session_id)
        assert isinstance(outcome, PreToolApprove)

    def test_mcp_tool_post_hook_with_autofix(self, handler, session_id, tmp_path):
        """Test MCP tool in post-hook with autofix enabled."""

        # Create a real config model with proper nested structure
        config = ModularConfig()
        config.hooks["post"] = PostToolHookConfig(auto_fix=True, autofix_categories=[AutofixCategory.FORMATTING])

        # Replace the config loader's config
        handler.config_loader._config = config

        # Create a real Python file
        test_file = tmp_path / "test.py"
        test_file.write_text("import os\nimport sys\n\n\ndef test():\n    pass")

        request = make_post_tool_request(
            session_id=session_id,
            tool_name="mcp_editor_save",
            tool_input={"file_path": test_file, "content": "import os\nimport sys\n\n\ndef test():\n    pass"},
            tool_result={"saved": True},
        )

        outcome = handler._handle_post_hook(request, session_id)

        # Should succeed since no violations
        assert isinstance(outcome, PostToolSuccess)

    def test_mcp_tool_name_variations(self, handler, session_id):
        """Test various MCP tool naming conventions."""
        tool_names = [
            "mcp__memory__search_nodes",  # Double underscore
            "mcp_memory_search",  # Single underscore
            "mcp-memory-search",  # Hyphen
            "mcpMemorySearch",  # CamelCase
            "MCP_MEMORY_SEARCH",  # Upper case
        ]

        for tool_name in tool_names:
            request = make_pre_tool_request(session_id=session_id, tool_name=tool_name, tool_input={"query": "test"})

            # All should be handled without errors
            outcome = handler._handle_pre_hook(request, session_id)
            assert isinstance(outcome, PreToolApprove)

    @pytest.mark.parametrize(
        ("tool_name", "input_fields", "should_check_python"),
        [
            # MCP tools - never checked for Python (only WriteToolCall is)
            ("mcp_memory_create", {"name": "test", "content": "data"}, False),
            ("mcp_browser_click", {"selector": "#button"}, False),
            ("mcp_fs_write", {"file_path": "test.py", "content": "print('hi')"}, False),
            ("mcp_editor_format", {"file_path": "app.py", "content": "x=1"}, False),
            ("mcp_tool", {"file_path": "test.js", "content": "console.log()"}, False),
            ("mcp_tool", {"file_path": None, "content": "print('hi')"}, False),
        ],
    )
    def test_mcp_tool_python_detection(self, handler, session_id, tool_name, input_fields, should_check_python):
        """Test that MCP tools are never detected as Python files (only WriteToolCall is checked)."""
        tool_input_dict = {"file_path": None, "content": None, **input_fields}

        request = make_pre_tool_request(session_id=session_id, tool_name=tool_name, tool_input=dict(**tool_input_dict))

        # MCP tools are never checked for Python - only WriteToolCall is
        is_python = handler._is_python_file(request)
        assert is_python == should_check_python

    def test_mcp_tool_session_tracking(self, handler, session_id):
        """Test that MCP tools properly track sessions."""
        request = make_pre_tool_request(
            session_id=session_id, tool_name="mcp_workspace_list", tool_input={"directory": "/home/user/project"}
        )

        # Process request via handle() which triggers session tracking
        handler.handle("PreToolUse", request)

        # Verify the session was tracked by checking the session file exists
        session_file = handler.session_manager._session_file(session_id)
        assert session_file.exists(), f"Session file was not created at {session_file}"

        # Also verify we can load the session data
        session_data = handler.session_manager._load_session(session_id)
        assert session_data.id == session_id
        assert session_data.last_seen is not None

    def test_mcp_tool_with_file_path_does_not_update_working_dir(self, handler, session_id):
        """Test that MCP tools do NOT update the working directory (only FilePathToolCall types do)."""
        request = make_pre_tool_request(
            session_id=session_id,
            tool_name="mcp_file_manager_open",
            tool_input={"file_path": Path("/home/user/projects/myapp/src/main.py"), "content": "# Main file"},
        )

        with patch.object(handler.session_manager, "track_session") as mock_track:
            handler._track_session(request, session_id)

            # MCP tools do NOT update working dir - only known FilePathToolCall types do
            # So it should use cwd instead
            mock_track.assert_called_once()
            call_args = mock_track.call_args[0]
            assert call_args[0] == session_id
            # Working dir should be cwd, not the file's parent
            assert call_args[1] == Path.cwd()


class TestMCPToolEdgeCases:
    """Test MCP tools with edge case inputs."""

    @pytest.fixture
    def handler(self):
        """Create a handler instance."""
        return HookHandler()

    def test_empty_tool_input(self, handler, session_id):
        """Test MCP tool with no input parameters."""
        request = make_pre_tool_request(session_id=session_id, tool_name="mcp_system_get_time", tool_input={})

        outcome = handler._handle_pre_hook(request, session_id)
        assert isinstance(outcome, PreToolApprove)

    def test_tool_with_none_values(self, handler, session_id):
        """Test tool with None values in various fields."""
        request = make_pre_tool_request(
            session_id=session_id,
            tool_name="mcp_data_processor",
            tool_input={
                "required_field": "value",
                "optional_field": None,
                "nested_object": {"key1": "value1", "key2": None, "key3": {"nested": None}},
                "array_with_nulls": [1, None, 3, None, 5],
                "completely_null": None,
            },
        )

        outcome = handler._handle_pre_hook(request, session_id)
        assert isinstance(outcome, PreToolApprove)

    def test_tool_with_special_characters(self, handler, session_id):
        """Test tool with special characters in field names."""
        request = make_pre_tool_request(
            session_id=session_id,
            tool_name="mcp-weird.tool$name",
            tool_input={
                "field-with-hyphens": "value",
                "field.with.dots": "value",
                "field$with$dollars": "value",
                "field@with@at": "value",
                "field with spaces": "value",
                "field_with_underscores": "value",
                "fieldWithCamelCase": "value",
                "FIELD_WITH_CAPS": "value",
                "123_numeric_start": "value",
                "field:with:colons": "value",
            },
        )

        outcome = handler._handle_pre_hook(request, session_id)
        assert isinstance(outcome, PreToolApprove)


class TestMCPToolLogging:
    """Test logging behavior for MCP tools."""

    @pytest.fixture
    def handler(self):
        """Create a handler with mocked logging."""
        handler = HookHandler()
        # Ensure log directory exists
        handler.log_dir.mkdir(parents=True, exist_ok=True)
        return handler

    def test_mcp_tool_logging(self, handler, session_id, tmp_path):
        """Test that MCP tool calls are properly logged."""
        # Override log directory
        handler.log_dir = tmp_path

        request = make_post_tool_request(
            session_id=session_id,
            tool_name="mcp_analytics_track",
            tool_input={
                "event": "user_action",
                "properties": {"action": "click", "target": "button"},
                "timestamp": datetime.now().isoformat(),
            },
            tool_result={"tracked": True},
        )

        # Process request
        handler._handle_post_hook(request, session_id)

        # Check log file was created
        log_file = tmp_path / f"{session_id}.log"
        assert log_file.exists()

        # Read and verify log content
        with log_file.open() as f:
            lines = f.readlines()

        # Should have decision logs and final log
        assert len(lines) >= 1

        # Parse the main log entry
        for line in lines:
            if line.startswith('{"timestamp"'):
                log_data = json.loads(line)
                assert log_data["hook_type"] == "PostToolUse"
                assert log_data["request"]["data"]["tool_name"] == "mcp_analytics_track"
                assert "event" in log_data["request"]["data"]["tool_input"]
                break

    def test_mcp_tool_decision_logging(self, handler, session_id, tmp_path):
        """Test that decision points are logged for MCP tools."""
        handler.log_dir = tmp_path

        request = make_pre_tool_request(
            session_id=session_id,
            tool_name="mcp_database_query",
            tool_input={"query": "SELECT * FROM users", "database": "postgres"},
        )

        # Process request
        handler._handle_pre_hook(request, session_id)

        # Read log file
        log_file = tmp_path / f"{session_id}.log"
        with log_file.open() as f:
            content = f.read()

        # Should have logged decision points
        assert "DECISION:" in content
        assert "pre_hook_start" in content
        assert "access_control" in content
        assert "file_type_check" in content


if __name__ == "__main__":
    pytest_bazel.main()
