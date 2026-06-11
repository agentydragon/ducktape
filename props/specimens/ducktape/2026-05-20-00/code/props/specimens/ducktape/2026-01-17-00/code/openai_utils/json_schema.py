"""Custom JSON schema generators for OpenAI strict mode compatibility.

Pydantic generates oneOf for discriminated unions, but OpenAI strict mode
doesn't support oneOf. This module provides a schema generator that converts
oneOf to anyOf while preserving discriminator metadata.

Additionally, OpenAI strict mode requires discriminator fields to be in the
required array, even when they have defaults. This generator detects Literal
fields with const values and marks them as required.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel
from pydantic.json_schema import GenerateJsonSchema, JsonSchemaValue
from pydantic_core import core_schema


class OpenAICompatibleSchema(GenerateJsonSchema):
    """Generate OpenAI strict mode compatible JSON schemas.

    This schema generator modifies Pydantic's default behavior to be compatible
    with OpenAI's strict mode requirements:

    - Converts oneOf to anyOf for discriminated unions (oneOf not supported)
    - Preserves discriminator metadata for proper validation
    - Marks Literal fields with const values as required (even with defaults)

    The last point is important for discriminated unions: fields like
    `type: Literal["http"] = "http"` are semantically required (must have
    exactly that value), but Pydantic treats them as optional because they
    have defaults. OpenAI strict mode requires discriminator fields in the
    required array for proper variant selection.

    Usage:
        from openai_utils.json_schema import openai_json_schema

        # Recommended: use the helper function
        schema = openai_json_schema(MyModel)

        # Or explicitly pass the schema generator
        schema = MyModel.model_json_schema(schema_generator=OpenAICompatibleSchema)

        # Or with TypeAdapter:
        adapter = TypeAdapter(MyType)
        schema = adapter.json_schema(schema_generator=OpenAICompatibleSchema)

    Note: This only affects the JSON schema representation. Pydantic validation
    behavior is unchanged - discriminated union validation still works perfectly.
    """

    def field_is_required(
        self, field: core_schema.ModelField | core_schema.DataclassField | core_schema.TypedDictField, total: bool
    ) -> bool:
        """Determine if a field should be in the required array.

        OpenAI strict mode requires ALL properties to be in the required array,
        even fields with defaults. This differs from JSON Schema convention where
        fields with defaults are typically optional.

        OpenAI's rule: "'required' is required to be supplied and to be an array
        including every key in properties."

        Rationale:
        - Discriminator fields: `type: Literal["http"] = "http"` must be in
          required for proper variant selection
        - Nullable fields: `headers: list[str] | None = None` must be in required
          even though they have defaults
        - All fields: OpenAI wants explicit presence, defaults are just conveniences

        This override marks ALL fields as required in the JSON schema, regardless
        of whether they have defaults in Python. The defaults are still present
        in the schema (for documentation/tooling), but fields are in required array.
        """
        # For OpenAI strict mode: all fields are required in the schema
        # Only TypedDict fields can be truly optional (when required=False)
        if field["type"] == "typed-dict-field":
            # Respect TypedDict's explicit required/optional
            return field.get("required", total)

        # All model/dataclass fields are required in the JSON schema for OpenAI
        # (even if they have defaults - that's just for convenient construction)
        return True

    def tagged_union_schema(self, schema: core_schema.TaggedUnionSchema) -> JsonSchemaValue:
        """Override to generate anyOf instead of oneOf for discriminated unions.

        Pydantic generates oneOf for discriminated unions by default, which matches
        OpenAPI conventions but isn't supported by OpenAI strict mode. This converts
        it to anyOf while keeping all the discriminator metadata intact.
        """
        json_schema = super().tagged_union_schema(schema)

        # Convert oneOf to anyOf if present
        if "oneOf" in json_schema:
            json_schema["anyOf"] = json_schema.pop("oneOf")

        return json_schema


def openai_json_schema(model: type[BaseModel]) -> dict[str, Any]:
    """Generate OpenAI-compatible JSON schema for a Pydantic model.

    This is a convenience wrapper around model_json_schema(schema_generator=OpenAICompatibleSchema)
    to avoid repetition throughout the codebase.

    Args:
        model: Pydantic BaseModel class to generate schema for

    Returns:
        JSON schema dict compatible with OpenAI structured outputs (anyOf instead of oneOf)
    """
    return model.model_json_schema(schema_generator=OpenAICompatibleSchema)
