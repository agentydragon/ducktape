"""Tests for OpenAI-compatible JSON schema generation."""

from __future__ import annotations

from typing import Annotated, Literal

import pytest
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from openai_utils.json_schema import OpenAICompatibleSchema
from openai_utils.pydantic_strict_mode import validate_openai_strict_mode_schema


class Cat(BaseModel):
    """Cat variant."""

    pet_type: Literal["cat"]
    meows: int
    model_config = ConfigDict(extra="forbid")


class Dog(BaseModel):
    """Dog variant."""

    pet_type: Literal["dog"]
    barks: float
    model_config = ConfigDict(extra="forbid")


class PetWithDiscriminator(BaseModel):
    """Model with discriminated union using custom schema generator.

    Without OpenAICompatibleSchema, this would generate oneOf which is rejected.
    With it, generates anyOf which is accepted.
    """

    animal: Annotated[Cat | Dog, Field(discriminator="pet_type")]
    model_config = ConfigDict(extra="forbid")


def test_default_pydantic_generates_oneof():
    """Verify that default Pydantic generates oneOf for discriminated unions."""
    schema = PetWithDiscriminator.model_json_schema()

    # Default Pydantic uses oneOf
    assert "oneOf" in schema["properties"]["animal"]
    assert "anyOf" not in schema["properties"]["animal"]

    # This should fail OpenAI strict mode validation
    with pytest.raises(Exception, match="oneOf"):
        validate_openai_strict_mode_schema(schema, "PetWithDiscriminator")


def test_custom_schema_generates_anyof():
    """Verify that OpenAICompatibleSchema converts oneOf to anyOf."""
    schema = PetWithDiscriminator.model_json_schema(schema_generator=OpenAICompatibleSchema)

    # Custom schema generator uses anyOf
    assert "anyOf" in schema["properties"]["animal"]
    assert "oneOf" not in schema["properties"]["animal"]

    # Discriminator metadata is preserved
    assert "discriminator" in schema["properties"]["animal"]
    assert schema["properties"]["animal"]["discriminator"]["propertyName"] == "pet_type"


def test_custom_schema_passes_strict_mode_validation():
    """Verify that anyOf discriminated unions pass OpenAI strict mode validation."""
    schema = PetWithDiscriminator.model_json_schema(schema_generator=OpenAICompatibleSchema)

    # This should pass validation (no exception)
    validate_openai_strict_mode_schema(schema, "PetWithDiscriminator")


def test_validation_still_works():
    """Verify that Pydantic validation works regardless of JSON schema format.

    The oneOf vs anyOf distinction is purely in the JSON schema representation.
    Validation behavior is driven by the core schema, not JSON schema.
    """
    # Create instances - validation works fine
    pet_cat = PetWithDiscriminator(animal=Cat(pet_type="cat", meows=5))
    assert pet_cat.animal.pet_type == "cat"
    assert isinstance(pet_cat.animal, Cat)

    pet_dog = PetWithDiscriminator(animal=Dog(pet_type="dog", barks=3.14))
    assert pet_dog.animal.pet_type == "dog"
    assert isinstance(pet_dog.animal, Dog)

    # Validation errors still work
    with pytest.raises(ValidationError):
        PetWithDiscriminator(animal={"pet_type": "bird", "chirps": 2})
