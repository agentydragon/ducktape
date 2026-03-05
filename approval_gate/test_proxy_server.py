import pytest_bazel

from approval_gate.models import BlockingWait, YieldAfterMs
from approval_gate.proxy_server import _resolve_effective_timeout, _wrap_tool_schema


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


# ── _resolve_effective_timeout ──────────────────────────────────────────────


def test_blocking_returns_inf():
    assert _resolve_effective_timeout(BlockingWait(), server_default=30.0) == float("inf")


def test_blocking_overrides_server_default():
    assert _resolve_effective_timeout(BlockingWait(), server_default=0.01) == float("inf")


def test_yield_after_ms_converts_to_seconds():
    assert _resolve_effective_timeout(YieldAfterMs(timeout_ms=5000), server_default=None) == 5.0


def test_yield_after_ms_overrides_server_default():
    assert _resolve_effective_timeout(YieldAfterMs(timeout_ms=2000), server_default=30.0) == 2.0


def test_yield_after_ms_clamps_negative_to_zero():
    assert _resolve_effective_timeout(YieldAfterMs(timeout_ms=-100), server_default=None) == 0


def test_yield_after_ms_zero_returns_zero():
    assert _resolve_effective_timeout(YieldAfterMs(timeout_ms=0), server_default=30.0) == 0


def test_none_falls_back_to_server_default():
    assert _resolve_effective_timeout(None, server_default=30.0) == 30.0


def test_none_with_none_default_returns_none():
    assert _resolve_effective_timeout(None, server_default=None) is None


if __name__ == "__main__":
    pytest_bazel.main()
