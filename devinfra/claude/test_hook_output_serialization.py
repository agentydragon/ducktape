"""Tests that hook output models serialize correctly for Claude Code's Zod validator.

Claude Code v2.1.76 uses Zod .optional() for optional fields, which accepts
undefined (field absent in JSON) but rejects null. Pydantic's default
model_dump_json() emits "field": null for None values, causing Zod validation
failures like:

    Hook JSON output validation failed:
      - stopReason: Expected string, received null
      - systemMessage: Expected string, received null

The fix is to use exclude_none=True when serializing. We can't use
exclude_unset because it would also drop Literal defaults like hookEventName
that Zod requires as discriminators.
"""

import json

import pytest_bazel

from devinfra.claude.claude_api.hooks.session_start import SessionStartHookSpecificOutput, SessionStartOutput


def test_exclude_none_omits_null_fields() -> None:
    """Fields with None values are omitted, avoiding null values that Zod
    .optional() rejects. Literal defaults like hookEventName are preserved."""
    output = SessionStartOutput(
        hook_specific_output=SessionStartHookSpecificOutput(additional_context="# Session hook context")
    )
    raw = output.model_dump_json(by_alias=True, exclude_none=True)
    parsed = json.loads(raw)
    # None fields must be absent, not null — Zod .optional() rejects null
    assert "stopReason" not in parsed
    assert "systemMessage" not in parsed
    # Non-None defaults are preserved
    assert parsed["continue"] is True
    assert parsed["suppressOutput"] is False
    # Literal defaults required by Zod as discriminators are preserved
    assert parsed["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    assert parsed["hookSpecificOutput"]["additionalContext"] == "# Session hook context"


def test_default_serialization_emits_nulls() -> None:
    """Demonstrates the bug: without exclude_none, None becomes null in JSON."""
    output = SessionStartOutput(hook_specific_output=SessionStartHookSpecificOutput(additional_context="ctx"))
    parsed = json.loads(output.model_dump_json(by_alias=True))
    # This is the broken behavior that Claude Code rejects
    assert parsed["stopReason"] is None
    assert parsed["systemMessage"] is None


if __name__ == "__main__":
    pytest_bazel.main()
