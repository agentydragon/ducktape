"""Tests for OpenAI strict mode validation."""

from pathlib import Path
from typing import Literal

import pytest
from pydantic import BaseModel, ConfigDict, Field

from openai_utils.json_schema import OpenAICompatibleSchema
from openai_utils.pydantic_strict_mode import (
    OpenAIStrictModeBaseModel,
    OpenAIStrictModeValidationError,
    validate_model,
    validate_openai_strict_mode_schema,
)
from openai_utils.testing.strict_mode_models import (
    DiscriminatedUnionModel,
    InvalidPathModel,
    InvalidSetModel,
    MissingAdditionalPropertiesModel,
    OptionalFieldModel,
    SimpleUnionModel,
    SimpleValidModel,
)


def test_valid_strict_mode_schema():
    """Valid schemas should pass validation (automatic via __init_subclass__)."""

    # Should not raise during class definition
    class ValidModel(OpenAIStrictModeBaseModel):
        # str not Path: OpenAI strict mode doesn't accept format="path"
        cwd: str | None = None
        # list not set: OpenAI strict mode doesn't accept uniqueItems
        files: list[str]
        max_bytes: int = Field(ge=0, le=100_000)

    # Model was created successfully, validation passed


def test_invalid_path_format():
    """Path types should be rejected (format='path') - automatic validation."""

    # Validation happens during class definition via __init_subclass__
    with pytest.raises(OpenAIStrictModeValidationError, match="format 'path'"):

        class InvalidModel(OpenAIStrictModeBaseModel):
            path: Path


def test_invalid_uniqueitems():
    """Set types should be rejected (uniqueItems) - automatic validation."""

    # Validation happens during class definition via __init_subclass__
    with pytest.raises(OpenAIStrictModeValidationError, match="uniqueItems"):

        class InvalidModel(OpenAIStrictModeBaseModel):
            tags: set[str]


def test_ref_with_description():
    """$ref with additional keywords should be rejected - automatic validation."""
    type MyUnion = list[str] | Literal["all"]

    # Validation happens during class definition via __init_subclass__
    with pytest.raises(OpenAIStrictModeValidationError, match=r"\$ref cannot have additional keywords"):

        class InvalidModel(OpenAIStrictModeBaseModel):
            # This generates $ref with description keyword
            files: MyUnion = Field(description="Files to process")


# Test models specific to this file (not shared across test files)
class NestedUnionModel(BaseModel):
    """Model with nested union (anyOf inside nested object - allowed)."""

    class ConfigValue(BaseModel):
        value: str | int
        model_config = ConfigDict(extra="forbid")

    config: ConfigValue
    model_config = ConfigDict(extra="forbid")


# Parameterized test cases: (model_class, should_pass, error_pattern)
VALIDATION_TEST_CASES = [
    # Valid cases
    pytest.param(SimpleValidModel, True, None, id="valid-basic"),
    pytest.param(SimpleUnionModel, True, None, id="valid-simple-union"),
    pytest.param(NestedUnionModel, True, None, id="valid-nested-union"),
    pytest.param(OptionalFieldModel, True, None, id="valid-optional-null"),
    pytest.param(DiscriminatedUnionModel, True, None, id="valid-discriminated-union-anyof"),
    # Invalid cases
    pytest.param(InvalidPathModel, False, "format 'path'", id="invalid-path-format"),
    pytest.param(InvalidSetModel, False, "uniqueItems", id="invalid-set-uniqueitems"),
    pytest.param(MissingAdditionalPropertiesModel, False, "additionalProperties", id="invalid-missing-extra-forbid"),
]


@pytest.mark.parametrize(("model_class", "should_pass", "error_pattern"), VALIDATION_TEST_CASES)
def test_validate_model_parameterized(model_class: type[BaseModel], should_pass: bool, error_pattern: str | None):
    """Parameterized test for validating Pydantic models against OpenAI strict mode."""
    # Use OpenAICompatibleSchema generator to match OpenAIStrictModeBaseModel behavior
    schema = model_class.model_json_schema(schema_generator=OpenAICompatibleSchema)
    model_name = model_class.__name__

    if should_pass:
        # Should not raise
        validate_openai_strict_mode_schema(schema, model_name)
    else:
        # Should raise with expected error pattern
        with pytest.raises(OpenAIStrictModeValidationError, match=error_pattern):
            validate_openai_strict_mode_schema(schema, model_name)


def test_validate_arbitrary_model():
    """validate_model() should work on arbitrary Pydantic models."""

    # Valid model (not subclassing OpenAIStrictModeBaseModel)
    class ValidArbitraryModel(BaseModel):
        name: str
        count: int = Field(ge=0, le=100)

        model_config = ConfigDict(extra="forbid")

    validate_model(ValidArbitraryModel)  # Should not raise

    # Invalid model with Path
    class InvalidArbitraryModel(BaseModel):
        path: Path

    with pytest.raises(OpenAIStrictModeValidationError, match="format 'path'"):
        validate_model(InvalidArbitraryModel)


def test_oneof_not_permitted():
    """oneOf is never allowed in strict mode - automatic validation."""

    # Schema with oneOf at property root level should be rejected
    schema_with_oneof = {
        "type": "object",
        "properties": {"value": {"oneOf": [{"type": "string"}, {"type": "integer"}]}},
        "required": ["value"],
        "additionalProperties": False,
    }

    with pytest.raises(OpenAIStrictModeValidationError, match="oneOf is not supported"):
        validate_openai_strict_mode_schema(schema_with_oneof, "OneOfSchema")


def test_anyof_at_property_level_is_permitted():
    """anyOf at property level IS allowed - only schema root restriction applies."""

    # Schema with anyOf at property level should be ACCEPTED
    # Per OpenAI docs: anyOf is allowed in properties, just not at schema root
    schema_with_anyof = {
        "type": "object",
        "properties": {
            "files": {
                "anyOf": [
                    {
                        "type": "object",
                        "properties": {
                            "kind": {"const": "specific"},
                            "paths": {"type": "array", "items": {"type": "string"}},
                        },
                        "required": ["kind", "paths"],
                        "additionalProperties": False,
                    },
                    {
                        "type": "object",
                        "properties": {"kind": {"const": "all"}},
                        "required": ["kind"],
                        "additionalProperties": False,
                    },
                ]
            }
        },
        "required": ["files"],
        "additionalProperties": False,
    }

    # Should not raise - anyOf at property level is allowed
    validate_openai_strict_mode_schema(schema_with_anyof, "AnyOfSchema")


def test_anyof_at_schema_root_not_permitted():
    """anyOf at schema ROOT is not allowed."""

    # Schema with anyOf at root level (no type: object) should be rejected
    schema_with_root_anyof = {
        "anyOf": [
            {
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
                "additionalProperties": False,
            },
            {
                "type": "object",
                "properties": {"id": {"type": "integer"}},
                "required": ["id"],
                "additionalProperties": False,
            },
        ]
    }

    with pytest.raises(OpenAIStrictModeValidationError, match="Schema root cannot use anyOf"):
        validate_openai_strict_mode_schema(schema_with_root_anyof, "RootAnyOfSchema")


def test_anyof_nested_is_permitted():
    """anyOf nested inside properties (not at root level) is allowed."""

    # Schema with anyOf nested inside an object property should be accepted
    schema_with_nested_anyof = {
        "type": "object",
        "properties": {
            "config": {
                "type": "object",
                "properties": {"value": {"anyOf": [{"type": "string"}, {"type": "integer"}]}},
                "required": ["value"],
                "additionalProperties": False,
            }
        },
        "required": ["config"],
        "additionalProperties": False,
    }

    # Should not raise - anyOf is allowed when nested
    validate_openai_strict_mode_schema(schema_with_nested_anyof, "NestedAnyOfSchema")
