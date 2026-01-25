"""Tests for OpenAI strict mode schema validation."""

from __future__ import annotations

import pytest
import pytest_bazel
from pydantic import BaseModel, ConfigDict, Field

from mcp_infra.enhanced.server import EnhancedFastMCP


@pytest.fixture
def mcp() -> EnhancedFastMCP:
    """Fresh EnhancedFastMCP server for each test."""
    return EnhancedFastMCP("openai_strict_test")


class StrictInput(BaseModel):
    """Input model with strict mode (additionalProperties: false)."""

    name: str
    value: int

    model_config = ConfigDict(extra="forbid")


class NonStrictInput(BaseModel):
    """Input model without strict mode (additionalProperties not set)."""

    name: str


class InputWithSet(BaseModel):
    """Input model with set field (produces uniqueItems: true)."""

    ids: set[str] = Field(description="Unique IDs")

    model_config = ConfigDict(extra="forbid")


class NestedModel(BaseModel):
    """Nested model referenced by InputWithNestedRef."""

    value: str

    model_config = ConfigDict(extra="forbid")


class InputWithNestedRefAndDescription(BaseModel):
    """Input model with $ref field that has description (not allowed).

    When a field references another model, Pydantic generates a $ref.
    Adding Field(description=...) adds keywords alongside $ref, which
    OpenAI strict mode doesn't allow.
    """

    # This produces: {"$ref": "...", "description": "..."} which is invalid
    nested: NestedModel = Field(description="Nested model reference")

    model_config = ConfigDict(extra="forbid")


class InputWithNestedSet(BaseModel):
    """Input with nested model that contains a set (uniqueItems in $defs)."""

    inner: InputWithSet

    model_config = ConfigDict(extra="forbid")


def test_strict_mode_validates_at_registration(mcp: EnhancedFastMCP):
    """OpenAI strict mode validation should happen at tool registration time.

    Uses @flat_model which preserves additionalProperties: false from Pydantic models.
    Regular @tool() goes through FastMCP introspection which strips it.
    """

    @mcp.flat_model()
    def strict_tool(input: StrictInput) -> str:
        """Tool with strict input model."""
        return f"{input.name}: {input.value}"

    # Should succeed - tool is registered and validated
    # (No exception means validation passed)


def test_strict_mode_rejects_non_strict_schema(mcp: EnhancedFastMCP):
    """OpenAI strict mode should reject schemas without additionalProperties: false."""
    # This should raise an error during registration because NonStrictInput
    # doesn't have extra="forbid" (which generates additionalProperties: false)
    with pytest.raises(ValueError, match="additionalProperties"):

        @mcp.flat_model()
        def non_strict_tool(input: NonStrictInput) -> str:
            """Tool with non-strict input model."""
            return input.name


def test_strict_mode_rejects_set_field(mcp: EnhancedFastMCP):
    """OpenAI strict mode should reject schemas with uniqueItems (from set types)."""
    # set[str] produces uniqueItems: true in JSON schema, which OpenAI doesn't allow
    with pytest.raises(ValueError, match="uniqueItems"):

        @mcp.flat_model()
        def set_tool(input: InputWithSet) -> str:
            """Tool with set field."""
            return str(input.ids)


def test_strict_mode_rejects_ref_with_description(mcp: EnhancedFastMCP):
    """OpenAI strict mode should reject $ref with additional keywords like description."""
    # Field(description=...) on a model reference produces:
    # {"$ref": "...", "description": "..."} which is invalid
    with pytest.raises(ValueError, match=r"\$ref cannot have additional keywords"):

        @mcp.flat_model()
        def ref_desc_tool(input: InputWithNestedRefAndDescription) -> str:
            """Tool with $ref + description."""
            return input.nested.value


def test_strict_mode_rejects_nested_set_in_defs(mcp: EnhancedFastMCP):
    """OpenAI strict mode should reject uniqueItems in nested models ($defs)."""
    # The inner model has set[str] which produces uniqueItems in $defs
    with pytest.raises(ValueError, match="uniqueItems"):

        @mcp.flat_model()
        def nested_set_tool(input: InputWithNestedSet) -> str:
            """Tool with nested model containing set."""
            return str(input.inner.ids)


if __name__ == "__main__":
    pytest_bazel.main()
