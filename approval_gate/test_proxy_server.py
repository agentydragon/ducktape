import pytest_bazel

from approval_gate.proxy_server import _wrap_tool_schema


def test_wraps_original_schema_under_input():
    original = {"type": "object", "properties": {"cmd": {"type": "string"}}, "required": ["cmd"]}
    result = _wrap_tool_schema(original)
    assert result["properties"]["input"] == original


def test_adds_justification_and_session_key_at_top_level():
    result = _wrap_tool_schema({})
    assert "justification" in result["properties"]
    assert "session_key" in result["properties"]


def test_required_contains_input_justification_and_session_key():
    result = _wrap_tool_schema({})
    assert "input" in result["required"]
    assert "justification" in result["required"]
    assert "session_key" in result["required"]


def test_does_not_mutate_original_schema():
    original: dict = {"properties": {"x": {"type": "string"}}, "required": ["x"]}
    _wrap_tool_schema(original)
    assert list(original["properties"].keys()) == ["x"]
    assert original["required"] == ["x"]


if __name__ == "__main__":
    pytest_bazel.main()
