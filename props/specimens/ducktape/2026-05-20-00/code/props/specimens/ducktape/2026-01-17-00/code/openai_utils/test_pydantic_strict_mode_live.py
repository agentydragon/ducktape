"""Live tests against OpenAI API to verify strict mode schema acceptance.

These tests make real API calls to verify what OpenAI actually accepts/rejects,
and validate that our checker matches OpenAI's behavior.
"""

from __future__ import annotations

import itertools
import os
from typing import Annotated, Literal

import pytest
from openai import BadRequestError
from pydantic import BaseModel, ConfigDict, Field
from pydantic.json_schema import GenerateJsonSchema

from openai_utils.client_factory import build_client
from openai_utils.json_schema import OpenAICompatibleSchema
from openai_utils.model import FunctionToolParam, ResponsesRequest, UserMessage
from openai_utils.pydantic_strict_mode import OpenAIStrictModeValidationError, validate_openai_strict_mode_schema
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


# Test models specific to this file (OpenAI API live tests)
class RefModelMissingAdditional(BaseModel):
    """Referenced model missing additionalProperties."""

    name: str
    # Intentionally no model_config


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


# Test models for accepted schemas (strict mode compliant)
class RefStrictModel(BaseModel):
    """Referenced model with additionalProperties."""

    name: str
    model_config = ConfigDict(extra="forbid")


class DefsWithAdditionalProperties(BaseModel):
    """Schema with $defs having additionalProperties."""

    input: RefStrictModel
    model_config = ConfigDict(extra="forbid")


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


# Generate test cases: (model_class, schema_generator, expected_result)
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
