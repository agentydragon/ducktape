"""Pydantic JSON schema adjustments for OpenAI strict mode."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel
from pydantic.json_schema import GenerateJsonSchema, JsonSchemaMode, JsonSchemaValue
from pydantic_core import core_schema

# Keys that are expected to be added as siblings to $ref by Pydantic
# (e.g., Field(description=...) adds "description" alongside $ref).
# These are safe to merge: the sibling value takes precedence.
_ALLOWED_REF_SIBLING_KEYS = {"description", "title", "default", "examples"}

_DEFS_REF_PREFIX = "#/$defs/"


def _inline_refs_with_siblings(schema: dict[str, Any]) -> None:
    """Inline local ``$defs`` refs with siblings; reject conflicting overrides."""
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
    """Adapt Pydantic schemas to OpenAI strict mode.

    Converts tagged unions from ``oneOf`` to ``anyOf`` while retaining
    discriminator metadata, makes model/dataclass fields required, and inlines
    local ``$defs`` refs with sibling keywords. TypedDict optionality remains
    governed by its ``required``/``total`` settings. These changes affect only
    the emitted schema; defaults remain in it even when model/dataclass fields
    are required, and Pydantic validation is unchanged.
    """

    def generate(self, schema: core_schema.CoreSchema, mode: JsonSchemaMode = "validation") -> JsonSchemaValue:
        json_schema = super().generate(schema, mode)
        _inline_refs_with_siblings(json_schema)
        return json_schema

    def field_is_required(
        self, field: core_schema.ModelField | core_schema.DataclassField | core_schema.TypedDictField, total: bool
    ) -> bool:
        """Require model/dataclass fields; preserve TypedDict optionality."""
        # For OpenAI strict mode: all fields are required in the schema
        # Only TypedDict fields can be truly optional (when required=False)
        if field["type"] == "typed-dict-field":
            # Respect TypedDict's explicit required/optional
            return field.get("required", total)

        # All model/dataclass fields are required in the JSON schema for OpenAI
        # (even if they have defaults - that's just for convenient construction)
        return True

    def tagged_union_schema(self, schema: core_schema.TaggedUnionSchema) -> JsonSchemaValue:
        """Use ``anyOf`` for tagged unions while retaining discriminator metadata."""
        json_schema = super().tagged_union_schema(schema)

        # Convert oneOf to anyOf if present
        if "oneOf" in json_schema:
            json_schema["anyOf"] = json_schema.pop("oneOf")

        return json_schema


def openai_json_schema(model: type[BaseModel]) -> dict[str, Any]:
    """Generate the OpenAI strict-mode schema for a Pydantic model."""
    return model.model_json_schema(schema_generator=OpenAICompatibleSchema)
