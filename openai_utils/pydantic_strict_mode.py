"""Validate schemas used by strict OpenAI tools and Pydantic models."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict

from openai_utils.json_schema import OpenAICompatibleSchema


class OpenAIStrictModeValidationError(ValueError):
    """Raised when a model's JSON schema is not OpenAI strict mode compatible."""


def validate_model(model_class: type[BaseModel]) -> None:
    """Validate a Pydantic model without subclassing OpenAIStrictModeBaseModel."""
    schema = model_class.model_json_schema()
    validate_openai_strict_mode_schema(schema, model_class.__name__)


def validate_openai_strict_mode_schema(schema: dict[str, Any], model_name: str = "Model") -> None:
    """Validate the strict fixed-property schema subset shared by tool inputs and Pydantic models."""
    errors: list[str] = []
    allowed_formats = {"date-time", "time", "date", "duration", "email", "hostname", "uri", "ipv4", "ipv6", "uuid"}

    if schema.get("type") != "object" or "anyOf" in schema:
        errors.append(f"{model_name}: Schema root must be an object and cannot use anyOf")

    def check_subschema(obj: dict[str, Any], path: str) -> None:
        schema_type = obj.get("type")
        is_object = schema_type == "object" or (isinstance(schema_type, list) and "object" in schema_type)
        if "oneOf" in obj:
            errors.append(f"{path}: oneOf is not supported in strict mode (use anyOf)")
        if "allOf" in obj:
            errors.append(f"{path}: allOf is not supported in strict mode")
        if "patternProperties" in obj:
            errors.append(f"{path}: patternProperties is outside our fixed-property tool subset")
        if is_object or "properties" in obj:
            if obj.get("additionalProperties") is not False:
                errors.append(f"{path}: Objects must have additionalProperties: false")
            properties = obj.get("properties", {})
            missing = set(properties) - set(obj.get("required", []))
            if missing:
                errors.append(f"{path}: All properties must be in 'required' array. Missing: {sorted(missing)}")
        if "format" in obj and obj["format"] not in allowed_formats:
            errors.append(f"{path}: Disallowed format '{obj['format']}' (use str type instead)")
        if obj.get("uniqueItems") is True:
            errors.append(f"{path}: uniqueItems not allowed (use list instead of set)")
        if "$ref" in obj and (extra_keys := set(obj) - {"$ref"}):
            errors.append(f"{path}: $ref cannot have additional keywords {sorted(extra_keys)}")

        # These keys hold schemas. Metadata such as default and examples does not.
        for key in ("properties", "$defs"):
            for name, child in obj.get(key, {}).items():
                if isinstance(child, dict):
                    check_subschema(child, f"{path}.{key}.{name}")
        if isinstance(obj.get("items"), dict):
            check_subschema(obj["items"], f"{path}.items")
        for key in ("anyOf", "oneOf", "allOf"):
            for index, child in enumerate(obj.get(key, [])):
                if isinstance(child, dict):
                    check_subschema(child, f"{path}.{key}[{index}]")

    check_subschema(schema, model_name)
    if errors:
        raise OpenAIStrictModeValidationError(
            f"OpenAI strict mode violations in {model_name}:\n" + "\n".join(f"  - {error}" for error in errors)
        )


class OpenAIStrictModeBaseModel(BaseModel):
    """Pydantic model whose generated schema is validated at class definition."""

    model_config = ConfigDict(extra="forbid")

    @classmethod
    def __pydantic_init_subclass__(cls, **kwargs: Any) -> None:
        """Validate after Pydantic initializes fields so model_json_schema() is available."""
        super().__pydantic_init_subclass__(**kwargs)
        cls.validate_openai_strict_mode()

    @classmethod
    def validate_openai_strict_mode(cls) -> None:
        """Validate the schema generated for OpenAI requests."""
        schema = cls.model_json_schema(schema_generator=OpenAICompatibleSchema)
        validate_openai_strict_mode_schema(schema, cls.__name__)
