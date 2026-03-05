import pytest
import pytest_bazel

from approval_gate.models import BlockingWait, YieldAfterMs
from approval_gate.proxy_server import _wait_mode_to_timeout, _wrap_tool_schema


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


def test_schema_includes_wait_mode():
    result = _wrap_tool_schema({})
    assert "wait_mode" in result["properties"]
    assert "wait_mode" not in result["required"]


# ── _wait_mode_to_timeout ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("wait_mode", "expected"),
    [
        pytest.param(BlockingWait(), float("inf"), id="blocking-inf"),
        pytest.param(YieldAfterMs(timeout_ms=5000), 5.0, id="yield-converts-to-seconds"),
        pytest.param(YieldAfterMs(timeout_ms=0), 0, id="yield-zero"),
        pytest.param(YieldAfterMs(timeout_ms=-100), 0, id="yield-negative-clamps-to-zero"),
    ],
)
def test_wait_mode_to_timeout(wait_mode, expected):
    assert _wait_mode_to_timeout(wait_mode) == expected


if __name__ == "__main__":
    pytest_bazel.main()
