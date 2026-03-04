import pytest_bazel

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


def test_schema_includes_background_and_yield_ms():
    result = _wrap_tool_schema({})
    assert "background" in result["properties"]
    assert "yield_ms" in result["properties"]
    assert "background" not in result["required"]
    assert "yield_ms" not in result["required"]


# ── _resolve_effective_timeout ──────────────────────────────────────────────


def test_background_returns_zero():
    assert (
        _resolve_effective_timeout(background=True, yield_ms=None, approval_timeout_seconds=None, server_default=30.0)
        == 0
    )


def test_background_overrides_yield_ms_and_approval_timeout():
    assert (
        _resolve_effective_timeout(background=True, yield_ms=5000, approval_timeout_seconds=10.0, server_default=30.0)
        == 0
    )


def test_yield_ms_converts_to_seconds():
    assert (
        _resolve_effective_timeout(background=False, yield_ms=5000, approval_timeout_seconds=None, server_default=None)
        == 5.0
    )


def test_yield_ms_overrides_approval_timeout():
    assert (
        _resolve_effective_timeout(background=False, yield_ms=2000, approval_timeout_seconds=10.0, server_default=30.0)
        == 2.0
    )


def test_yield_ms_clamps_negative_to_zero():
    assert (
        _resolve_effective_timeout(background=False, yield_ms=-100, approval_timeout_seconds=None, server_default=None)
        == 0
    )


def test_approval_timeout_overrides_server_default():
    assert (
        _resolve_effective_timeout(background=False, yield_ms=None, approval_timeout_seconds=5.0, server_default=30.0)
        == 5.0
    )


def test_falls_back_to_server_default():
    assert (
        _resolve_effective_timeout(background=False, yield_ms=None, approval_timeout_seconds=None, server_default=30.0)
        == 30.0
    )


def test_all_none_returns_none():
    assert (
        _resolve_effective_timeout(background=False, yield_ms=None, approval_timeout_seconds=None, server_default=None)
        is None
    )


if __name__ == "__main__":
    pytest_bazel.main()
