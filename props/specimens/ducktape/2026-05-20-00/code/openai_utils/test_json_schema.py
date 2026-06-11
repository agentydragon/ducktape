"""Tests for OpenAI-compatible JSON schema generation."""

from __future__ import annotations

from typing import Annotated, Any, Literal, NewType

import pytest
import pytest_bazel
from pydantic import BaseModel, ConfigDict, Field, PlainSerializer, StringConstraints, ValidationError

from openai_utils.json_schema import OpenAICompatibleSchema, _inline_refs_with_siblings
from openai_utils.pydantic_strict_mode import OpenAIStrictModeBaseModel, validate_openai_strict_mode_schema


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


# ---------------------------------------------------------------------------
# _inline_refs_with_siblings: unit tests for edge cases not easily reproduced
# by real Pydantic models
# ---------------------------------------------------------------------------


def test_inline_ref_conflict_raises():
    """Sibling overwriting a non-allowed def key with a different value raises."""
    schema = {
        "type": "object",
        "properties": {"val": {"$ref": "#/$defs/Foo", "type": "integer"}},
        "$defs": {"Foo": {"type": "string"}},
    }
    with pytest.raises(ValueError, match=r"conflict.*type.*integer.*string"):
        _inline_refs_with_siblings(schema)


def test_inline_ref_unknown_def_left_alone():
    """$ref pointing to a non-existent def is left unchanged."""
    schema: dict[str, Any] = {
        "type": "object",
        "properties": {"x": {"$ref": "#/$defs/Missing", "description": "hmm"}},
        "$defs": {},
    }
    _inline_refs_with_siblings(schema)
    assert schema["properties"]["x"] == {"$ref": "#/$defs/Missing", "description": "hmm"}


def test_inline_ref_non_defs_path_left_alone():
    """$ref with a non-#/$defs/ path is not inlined even with siblings."""
    schema: dict[str, Any] = {
        "type": "object",
        "properties": {"x": {"$ref": "https://example.com/schema.json", "description": "external"}},
    }
    _inline_refs_with_siblings(schema)
    assert schema["properties"]["x"]["$ref"] == "https://example.com/schema.json"


# ---------------------------------------------------------------------------
# Integration tests: real Pydantic models exercising ref inlining
# ---------------------------------------------------------------------------

_STR_IDENTITY_SERIALIZER = PlainSerializer(lambda x: x, return_type=str, when_used="json")

# Constrained NewType — mirrors SnapshotSlug from props/core/ids.py
type _SnapshotSlugBase = Annotated[
    str, StringConstraints(pattern=r"^[a-z0-9_-]+/[a-z0-9_-]+$", min_length=3, max_length=100), _STR_IDENTITY_SERIALIZER
]
TestSnapshotSlug = NewType("TestSnapshotSlug", _SnapshotSlugBase)

# Plain NewType over Annotated[str, ...]
type _IssueIdBase = Annotated[str, StringConstraints(min_length=5, max_length=40)]
TestIssueId = NewType("TestIssueId", _IssueIdBase)


class ModelWithDescribedNewType(OpenAIStrictModeBaseModel):
    """Reproduces the original SingleFileSetExample pattern: NewType + Field(description)."""

    snapshot_slug: TestSnapshotSlug = Field(description="Snapshot to evaluate")
    name: str = Field(description="A plain field")


def test_newtype_with_description_passes_strict_mode():
    """NewType(Annotated[str, constraints]) + Field(description) passes strict mode."""
    schema = ModelWithDescribedNewType.model_json_schema(schema_generator=OpenAICompatibleSchema)
    validate_openai_strict_mode_schema(schema, "ModelWithDescribedNewType")

    slug_prop = schema["properties"]["snapshot_slug"]
    assert slug_prop["description"] == "Snapshot to evaluate"
    assert "$ref" not in slug_prop
    assert slug_prop["type"] == "string"
    # String constraints from the NewType are preserved
    assert slug_prop["minLength"] == 3
    assert slug_prop["maxLength"] == 100
    assert "pattern" in slug_prop


def test_newtype_without_description_uses_bare_ref():
    """NewType field WITHOUT Field(description) keeps $ref (no siblings, no inlining needed)."""

    class ModelNoDesc(OpenAIStrictModeBaseModel):
        slug: TestSnapshotSlug

    schema = ModelNoDesc.model_json_schema(schema_generator=OpenAICompatibleSchema)
    validate_openai_strict_mode_schema(schema, "ModelNoDesc")
    # Bare $ref is valid for OpenAI strict mode — no inlining needed
    assert "$ref" in schema["properties"]["slug"]


def test_newtype_default_generator_produces_ref_with_siblings():
    """Default Pydantic generator produces the $ref + description pattern our generator fixes."""
    schema = ModelWithDescribedNewType.model_json_schema()
    slug_prop = schema["properties"]["snapshot_slug"]
    assert "$ref" in slug_prop
    assert "description" in slug_prop


class ModelWithMultipleDescribedNewtypes(OpenAIStrictModeBaseModel):
    """Multiple NewType fields with descriptions — all should be inlined."""

    snapshot: TestSnapshotSlug = Field(description="Target snapshot")
    issue_id: TestIssueId = Field(description="Issue identifier")
    label: str = Field(description="Human-readable label")


def test_multiple_newtype_fields_with_descriptions():
    """Multiple NewType fields each with description all pass strict mode."""
    schema = ModelWithMultipleDescribedNewtypes.model_json_schema(schema_generator=OpenAICompatibleSchema)
    validate_openai_strict_mode_schema(schema, "ModelWithMultipleDescribedNewtypes")

    assert schema["properties"]["snapshot"]["description"] == "Target snapshot"
    assert "$ref" not in schema["properties"]["snapshot"]
    assert schema["properties"]["issue_id"]["description"] == "Issue identifier"
    assert "$ref" not in schema["properties"]["issue_id"]
    # Plain str field is unaffected (never had $ref)
    assert schema["properties"]["label"]["description"] == "Human-readable label"


class Inner(BaseModel):
    """Nested model referenced via $ref."""

    value: str
    model_config = ConfigDict(extra="forbid")


class ModelWithDescribedNestedModel(OpenAIStrictModeBaseModel):
    """Field typed as another BaseModel + description — produces $ref with description."""

    config: Inner = Field(description="Configuration object")
    name: str


def test_nested_model_with_description_inlined():
    """BaseModel field + Field(description) is inlined, preserving all nested structure."""
    schema = ModelWithDescribedNestedModel.model_json_schema(schema_generator=OpenAICompatibleSchema)
    validate_openai_strict_mode_schema(schema, "ModelWithDescribedNestedModel")

    config_prop = schema["properties"]["config"]
    assert "$ref" not in config_prop
    assert config_prop["description"] == "Configuration object"
    # The nested model's structure is preserved
    assert config_prop["type"] == "object"
    assert "value" in config_prop["properties"]
    assert config_prop["additionalProperties"] is False


class ModelWithOptionalNewType(OpenAIStrictModeBaseModel):
    """Optional NewType field with description — produces anyOf with $ref."""

    slug: TestSnapshotSlug | None = Field(default=None, description="Optional snapshot")


def test_optional_newtype_with_description():
    """Optional NewType + description: description is sibling of anyOf, not of $ref — valid."""
    schema = ModelWithOptionalNewType.model_json_schema(schema_generator=OpenAICompatibleSchema)
    validate_openai_strict_mode_schema(schema, "ModelWithOptionalNewType")

    slug_prop = schema["properties"]["slug"]
    assert slug_prop["description"] == "Optional snapshot"
    # The $ref inside anyOf is bare (no siblings) — valid for OpenAI strict mode
    assert "anyOf" in slug_prop


class ModelWithListOfNewType(OpenAIStrictModeBaseModel):
    """list[NewType] field with description — $ref appears in items."""

    slugs: list[TestSnapshotSlug] = Field(description="List of snapshots")


def test_list_of_newtype_with_description():
    """list[NewType] + description: the field-level description is on the array, not on items."""
    schema = ModelWithListOfNewType.model_json_schema(schema_generator=OpenAICompatibleSchema)
    validate_openai_strict_mode_schema(schema, "ModelWithListOfNewType")

    slugs_prop = schema["properties"]["slugs"]
    assert slugs_prop["description"] == "List of snapshots"
    assert slugs_prop["type"] == "array"


# Reproduces the exact SingleFileSetExample / WholeSnapshotExample pattern
class ExampleFileSet(OpenAIStrictModeBaseModel):
    """Exact reproduction of SingleFileSetExample from props."""

    model_config = ConfigDict(frozen=True)
    kind: Literal["file_set"] = "file_set"
    snapshot_slug: TestSnapshotSlug = Field(description="Snapshot to evaluate")
    files_hash: str = Field(description="File set hash")


class ExampleWholeSnapshot(OpenAIStrictModeBaseModel):
    """Exact reproduction of WholeSnapshotExample from props."""

    model_config = ConfigDict(frozen=True)
    kind: Literal["whole_snapshot"] = "whole_snapshot"
    snapshot_slug: TestSnapshotSlug = Field(description="Snapshot to evaluate")


def test_discriminated_union_with_described_newtypes():
    """Discriminated union where each variant has NewType + description fields."""

    class Container(OpenAIStrictModeBaseModel):
        example: Annotated[ExampleWholeSnapshot | ExampleFileSet, Field(discriminator="kind")]

    schema = Container.model_json_schema(schema_generator=OpenAICompatibleSchema)
    validate_openai_strict_mode_schema(schema, "Container")

    # No $ref with siblings anywhere in the schema
    def assert_no_ref_with_siblings(obj: object, path: str = "") -> None:
        if isinstance(obj, dict):
            if "$ref" in obj:
                assert len(obj) == 1, f"$ref with siblings at {path}: {obj}"
            for k, v in obj.items():
                assert_no_ref_with_siblings(v, f"{path}.{k}")
        elif isinstance(obj, list):
            for i, item in enumerate(obj):
                assert_no_ref_with_siblings(item, f"{path}[{i}]")

    assert_no_ref_with_siblings(schema)


if __name__ == "__main__":
    pytest_bazel.main()
