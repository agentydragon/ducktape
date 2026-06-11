"""Tests for OpenAI strict mode validation."""

from __future__ import annotations

import itertools
import os
from pathlib import Path
from typing import Annotated, Literal

import pytest
import pytest_bazel
from openai import BadRequestError
from pydantic import BaseModel, ConfigDict, Field
from pydantic.json_schema import GenerateJsonSchema

from openai_utils.client_factory import build_client
from openai_utils.json_schema import OpenAICompatibleSchema
from openai_utils.model import FunctionToolParam, ResponsesRequest, UserMessage
from openai_utils.pydantic_strict_mode import (
    OpenAIStrictModeBaseModel,
    OpenAIStrictModeValidationError,
    validate_model,
    validate_openai_strict_mode_schema,
)
from openai_utils.testing.strict_mode_models import (
    DiscriminatedUnionModel,
    DiscriminatedUnionWithDefaults,
    InvalidPathModel,
    InvalidSetModel,
    MissingAdditionalPropertiesModel,
    NestedMissingAdditionalPropertiesModel,
    NestedValidModel,
    OptionalFieldModel,
    SimpleUnionModel,
    SimpleValidModel,
)

# ---------------------------------------------------------------------------
# Test models specific to this file
# ---------------------------------------------------------------------------


class NestedUnionModel(BaseModel):
    """Model with nested union (anyOf inside nested object - allowed)."""

    class ConfigValue(BaseModel):
        value: str | int
        model_config = ConfigDict(extra="forbid")

    config: ConfigValue
    model_config = ConfigDict(extra="forbid")


class RefModelMissingAdditional(BaseModel):
    """Referenced model missing additionalProperties."""

    name: str


class MissingAdditionalPropertiesInDefs(BaseModel):
    """Missing additionalProperties in $defs."""

    input: RefModelMissingAdditional
    model_config = ConfigDict(extra="forbid")


class SpecificFiles(BaseModel):
    """Files specified by paths."""

    kind: Literal["specific"]
    paths: list[str]
    model_config = ConfigDict(extra="forbid")


class AllFiles(BaseModel):
    """All files."""

    kind: Literal["all"]
    model_config = ConfigDict(extra="forbid")


class AnyOfAtPropertyRoot(BaseModel):
    """Discriminated union at property level.

    This generates anyOf (not oneOf) at property level when using
    OpenAICompatibleSchema. This is ALLOWED in OpenAI strict mode - the restriction
    is only that the schema ROOT cannot be anyOf.
    """

    files: Annotated[SpecificFiles | AllFiles, Field(discriminator="kind")]
    model_config = ConfigDict(extra="forbid")


class RefStrictModel(BaseModel):
    """Referenced model with additionalProperties."""

    name: str
    model_config = ConfigDict(extra="forbid")


class DefsWithAdditionalProperties(BaseModel):
    """Schema with $defs having additionalProperties."""

    input: RefStrictModel
    model_config = ConfigDict(extra="forbid")


# ---------------------------------------------------------------------------
# Unit tests (mock — no API calls)
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Cross-generator parameterized tests (mock + live)
# ---------------------------------------------------------------------------

# Schema generator options
GENERATORS = [
    ("default", GenerateJsonSchema),  # Pydantic's default (generates oneOf for discriminated unions)
    ("openai", OpenAICompatibleSchema),  # Our custom generator (converts oneOf → anyOf)
]

# Models that are always rejected (regardless of generator)
ALWAYS_REJECTED = [
    MissingAdditionalPropertiesModel,
    NestedMissingAdditionalPropertiesModel,
    MissingAdditionalPropertiesInDefs,
    InvalidSetModel,
    InvalidPathModel,
]

# Models with discriminated unions (behavior depends on generator)
# - With default generator: oneOf → rejected
# - With OpenAI generator: anyOf → accepted (usually)
# - DiscriminatedUnionWithDefaults: discriminator field has default (not in required array)
DISCRIMINATED_UNIONS: list[type[BaseModel]] = [
    AnyOfAtPropertyRoot,
    DiscriminatedUnionModel,
    DiscriminatedUnionWithDefaults,
]

# Models with fields that have defaults (generator-dependent)
# - With default generator: fields with defaults omitted from required → rejected
# - With OpenAI generator: all fields in required → accepted
# OpenAI strict mode requires ALL properties in required array, even with defaults
FIELDS_WITH_DEFAULTS = [OptionalFieldModel]

# Models that are always accepted (regardless of generator)
ALWAYS_ACCEPTED = [SimpleValidModel, NestedValidModel, DefsWithAdditionalProperties, SimpleUnionModel]


def _make_test_cases():
    """Generate parameterized test cases using itertools.product."""
    cases = []

    # Always rejected x all generators
    for model, (gen_name, gen_class) in itertools.product(ALWAYS_REJECTED, GENERATORS):
        cases.append((model, gen_class, "reject", gen_name))

    # Discriminated unions x generators (generator-dependent behavior)
    for model, (gen_name, gen_class) in itertools.product(DISCRIMINATED_UNIONS, GENERATORS):
        expected = "reject" if gen_name == "default" else "accept"
        cases.append((model, gen_class, expected, gen_name))

    # Fields with defaults x generators (generator-dependent behavior)
    # Default generator omits fields with defaults from required → rejected by OpenAI
    # OpenAI generator includes all fields in required → accepted
    for model, (gen_name, gen_class) in itertools.product(FIELDS_WITH_DEFAULTS, GENERATORS):
        expected = "reject" if gen_name == "default" else "accept"
        cases.append((model, gen_class, expected, gen_name))

    # Always accepted x all generators
    for model, (gen_name, gen_class) in itertools.product(ALWAYS_ACCEPTED, GENERATORS):
        cases.append((model, gen_class, "accept", gen_name))

    return cases


TEST_SCHEMAS = _make_test_cases()


def _test_id(val):
    """Generate test IDs for parameterized tests."""
    if isinstance(val, tuple) and len(val) == 4:
        model, _, _, gen_name = val
        return f"{model.__name__}-{gen_name}"
    return None


@pytest.fixture
async def openai_client():
    """Create OpenAI Responses client with our wrappers."""
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        pytest.skip("OPENAI_API_KEY not set")
    return build_client("gpt-5.1-codex-mini")


@pytest.mark.parametrize(("model_class", "schema_generator", "expected", "gen_name"), TEST_SCHEMAS, ids=_test_id)
def test_validator_prediction_matches_expectation(
    model_class: type[BaseModel],
    schema_generator: type[GenerateJsonSchema],
    expected: Literal["accept", "reject"],
    gen_name: str,  # Used for test ID generation only
):
    """Test that our validator behavior matches our prediction for all schemas.

    Verifies: our validator behavior === expected behavior (acceptance/rejection)
    """
    schema = model_class.model_json_schema(schema_generator=schema_generator)
    model_name = model_class.__name__

    if expected == "reject":
        # We predict rejection - our validator should reject
        with pytest.raises(OpenAIStrictModeValidationError):
            validate_openai_strict_mode_schema(schema, model_name=model_name)
    else:
        # We predict acceptance - our validator should accept (no exception)
        validate_openai_strict_mode_schema(schema, model_name=model_name)


@pytest.mark.live_openai_api
@pytest.mark.parametrize(("model_class", "schema_generator", "expected", "gen_name"), TEST_SCHEMAS, ids=_test_id)
async def test_validator_matches_openai_reality(
    openai_client,
    model_class: type[BaseModel],
    schema_generator: type[GenerateJsonSchema],
    expected: Literal["accept", "reject"],
    gen_name: str,  # Used for test ID generation only
):
    """Test that our validator matches OpenAI's actual behavior.

    Verifies the critical property: our_validator === openai_api_reality
    This is the most important test - our validator must predict what OpenAI actually does.
    """
    schema = model_class.model_json_schema(schema_generator=schema_generator)
    model_name = model_class.__name__

    # Check our validator
    validator_error = None
    try:
        validate_openai_strict_mode_schema(schema, model_name=model_name)
    except OpenAIStrictModeValidationError as e:
        validator_error = e

    # Check OpenAI API
    openai_error = None
    try:
        result = await openai_client.responses_create(
            ResponsesRequest(
                input=[UserMessage.text("Test")],
                tools=[
                    FunctionToolParam(name="test_function", description="Test function", parameters=schema, strict=True)
                ],
            )
        )
        assert result is not None
    except BadRequestError as e:
        openai_error = e

    # Our validator MUST match OpenAI's behavior
    validator_accepts = validator_error is None
    openai_accepts = openai_error is None
    error_context = f"\nOpenAI error: {openai_error}" if openai_error else ""
    assert validator_accepts == openai_accepts, (
        f"Validator: {validator_accepts}, OpenAI: {openai_accepts}{error_context}"
    )


if __name__ == "__main__":
    pytest_bazel.main()
