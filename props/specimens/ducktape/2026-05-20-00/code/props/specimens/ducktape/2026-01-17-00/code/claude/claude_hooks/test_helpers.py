"""Shared test helpers for Claude Code hook input parsing tests."""

import json
import uuid
from importlib import resources
from pathlib import Path
from typing import Any, cast

from claude_hooks.inputs import PostToolInput


def load_test_json(tool_name: str, scenario: str) -> dict[str, Any]:
    """Load test JSON data from testdata directory."""
    try:
        testdata_path = resources.files("claude_hooks") / "testdata"
        json_path = testdata_path / "hook_inputs" / "PostToolUse" / tool_name / f"{scenario}.json"
        json_content = json_path.read_text()
        return cast(dict[str, Any], json.loads(json_content))
    except Exception as e:
        raise FileNotFoundError(f"Could not load test data for {tool_name}/{scenario}: {e}") from e


def create_test_session_id(test_name: str) -> uuid.UUID:
    """Create a deterministic UUID for test cases based on test name."""
    # Use namespace UUID to create consistent UUIDs for tests
    namespace = uuid.UUID("12345678-1234-5678-9abc-123456789abc")
    return uuid.uuid5(namespace, test_name)


def assert_tool_input_parsing(raw_json: dict[str, Any], expected_tool_input: Any, description: str = "") -> None:
    """Test that a raw JSON input parses correctly to expected tool input.

    Args:
        raw_json: Raw JSON dictionary to parse
        expected_tool_input: Expected parsed tool_input object
        description: Optional description for assertion failures
    """
    # Parse using Pydantic
    parsed_input = PostToolInput.model_validate(raw_json)

    # Check that tool_input was parsed to the expected type and values
    assert parsed_input.tool_input == expected_tool_input, (
        f"Tool input parsing failed{': ' + description if description else ''}\n"
        f"Expected: {expected_tool_input!r}\n"
        f"Got: {parsed_input.tool_input!r}"
    )

    # Also verify the full parsed object structure matches raw JSON structure
    expected_full = PostToolInput(
        session_id=uuid.UUID(raw_json["session_id"]),
        transcript_path=Path(raw_json["transcript_path"]),
        cwd=Path(raw_json["cwd"]),
        hook_event_name=raw_json["hook_event_name"],
        tool_name=raw_json["tool_name"],
        tool_input=expected_tool_input,
        tool_response=raw_json.get("tool_response"),
    )

    assert parsed_input == expected_full, f"Full object parsing failed{': ' + description if description else ''}"


def load_and_test_tool_scenario(tool_name: str, scenario: str, expected_tool_input: Any, description: str = "") -> None:
    """Load test JSON and verify it parses to expected tool input.

    Args:
        tool_name: Tool name (e.g. 'Bash', 'Read')
        scenario: Scenario name (e.g. 'command_only')
        expected_tool_input: Expected parsed tool_input object
        description: Optional description for test
    """
    raw_json = load_test_json(tool_name, scenario)
    assert_tool_input_parsing(raw_json, expected_tool_input, description)
