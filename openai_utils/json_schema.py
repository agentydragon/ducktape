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
from pydantic.json_schema import GenerateJsonSchema, JsonSchemaMode, JsonSchemaValue
from pydantic_core import core_schema

# Keys that are expected to be added as siblings to $ref by Pydantic
# (e.g., Field(description=...) adds "description" alongside $ref).
# These are safe to merge: the sibling value takes precedence.
_ALLOWED_REF_SIBLING_KEYS = frozenset({"description", "title", "default", "examples"})

_DEFS_REF_PREFIX = "#/$defs/"


def _inline_refs_with_siblings(schema: dict[str, Any]) -> None:
    """Inline $ref nodes that have sibling keywords.

    OpenAI strict mode forbids additional keywords alongside $ref
    (e.g., {"$ref": "#/$defs/Foo", "description": "..."}). This resolves
    such references by replacing the node with the inlined definition
    merged with the sibling keywords.

    Raises if a sibling key would silently overwrite a different value
    from the definition (except for known-safe keys like description/title).
    """
    defs = schema.get("$defs", {})

    def resolve(obj: Any, path: str = "") -> Any:
        if isinstance(obj, list):
            return [resolve(item, f"{path}[{i}]") for i, item in enumerate(obj)]
        if not isinstance(obj, dict):
            return obj

        # No $ref or no siblings — just recurse into values
        if "$ref" not in obj or len(obj) <= 1:
            return {k: resolve(v, f"{path}.{k}") for k, v in obj.items()}

        # $ref with siblings — try to inline
        ref_path = obj["$ref"]
        def_name = ref_path.removeprefix(_DEFS_REF_PREFIX)
        if def_name == ref_path or def_name not in defs:
            # Non-local or unknown ref — leave as-is
            return {k: resolve(v, f"{path}.{k}") for k, v in obj.items()}

        resolved = dict(defs[def_name])
        for key, value in obj.items():
            if key == "$ref":
                continue
            if key in resolved and resolved[key] != value and key not in _ALLOWED_REF_SIBLING_KEYS:
                raise ValueError(
                    f"$ref inlining conflict at {path}: "
                    f"sibling key {key!r} has value {value!r} but "
                    f"$defs/{def_name} already has {resolved[key]!r}"
                )
            resolved[key] = value
        return resolve(resolved, path)

    resolved = resolve(schema)
    schema.clear()
    schema.update(resolved)


class OpenAICompatibleSchema(GenerateJsonSchema):
    """Generate OpenAI strict mode compatible JSON schemas.

    This schema generator modifies Pydantic's default behavior to be compatible
    with OpenAI's strict mode requirements:

    - Converts oneOf to anyOf for discriminated unions (oneOf not supported)
    - Preserves discriminator metadata for proper validation
    - Marks Literal fields with const values as required (even with defaults)
    - Inlines $ref nodes that have sibling keywords (e.g., description alongside $ref)

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

    def generate(self, schema: core_schema.CoreSchema, mode: JsonSchemaMode = "validation") -> JsonSchemaValue:
        json_schema = super().generate(schema, mode)
        _inline_refs_with_siblings(json_schema)
        return json_schema

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
