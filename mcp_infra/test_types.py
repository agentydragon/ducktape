"""Tests for MCP domain types validation.

MCPMountPrefix declares its pattern twice (`_adapter` and
`__get_pydantic_core_schema__`); validating through the TypeAdapter chains
through both, so these examples tie the two mirrored declarations together.
The rejects pin the tool-name-safety choices ({prefix}_{tool} embedding).
"""

from __future__ import annotations

import pytest
import pytest_bazel
from pydantic import TypeAdapter, ValidationError

from mcp_infra.prefix import MCPMountPrefix

adapter = TypeAdapter(MCPMountPrefix)


class TestMCPMountPrefix:
    """Test MCPMountPrefix validation constraints."""

    @pytest.mark.parametrize(
        "valid_name",
        [
            "a",  # Single letter (minimum)
            "runtime",  # Common pattern
            "agent_123",  # With digits
            "my_server_2",  # Multiple underscores
            "run__time",  # Double underscores allowed (old restriction removed)
            "policy_reader",  # Real-world example
            "compositor_meta",  # Real-world example
        ],
    )
    def test_valid_names(self, valid_name: str):
        """Valid mount prefixes pass validation unchanged."""
        result = adapter.validate_python(valid_name)
        assert result == valid_name

    @pytest.mark.parametrize(
        ("invalid_name", "reason"),
        [
            ("", "empty string"),
            ("Runtime", "uppercase letter"),
            ("_runtime", "starts with underscore"),
            ("9runtime", "starts with digit"),
            ("runtime-exec", "contains hyphen"),
            ("a" * 51, "exceeds max length"),
            ("run time", "contains space"),
            ("run.time", "contains dot"),
            ("run/time", "contains slash"),
        ],
    )
    def test_invalid_names(self, invalid_name: str, reason: str):
        """Invalid mount prefixes raise ValidationError."""
        with pytest.raises(ValidationError):
            adapter.validate_python(invalid_name)


if __name__ == "__main__":
    pytest_bazel.main()
