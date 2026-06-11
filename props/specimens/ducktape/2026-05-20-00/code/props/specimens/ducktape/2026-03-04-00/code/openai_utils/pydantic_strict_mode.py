"""OpenAI strict mode validation for Pydantic models.

Provides validation for Pydantic models to ensure they conform to OpenAI's strict mode requirements
for function calling and structured outputs.

References:
- Function calling: https://platform.openai.com/docs/guides/function-calling
- Structured outputs: https://platform.openai.com/docs/guides/structured-outputs

OpenAI Strict Mode Requirements:
- additionalProperties must be false for all objects
- All fields must be required (use `| None` for optional fields)
- Only specific format values allowed: date-time, email, uri, uuid, ipv4, ipv6
- No uniqueItems (use list instead of set)
- No oneOf (not supported at all - use anyOf or flatten)
- No anyOf at root level of properties (flatten discriminated unions into single objects)
- No $ref with additional keywords (e.g., no Field(description=...) on type aliases)

Common patterns:
- Use `str` instead of `Path` (format="path" not allowed)
- Use `list[T]` instead of `set[T]` (uniqueItems not allowed)
- Use `field: Type | None = None` for optional fields (all fields must be in required array)
- Don't wrap type aliases with Field(description=...) (causes $ref with additional keywords)
- Flatten discriminated unions at property root level using optional fields + discriminator
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict

from openai_utils.json_schema import OpenAICompatibleSchema


class OpenAIStrictModeValidationError(ValueError):
    """Raised when a model's JSON schema is not OpenAI strict mode compatible."""


def validate_model(model_class: type[BaseModel]) -> None:
    """Validate that a Pydantic model's JSON schema is OpenAI strict mode compatible.

    Convenience function to validate arbitrary Pydantic models without subclassing
    OpenAIStrictModeBaseModel. Use this for one-off validation or models you don't control.

    Example:
        from pydantic import BaseModel
        from openai_utils.pydantic_strict_mode import validate_model

        class MyModel(BaseModel):
            name: str
            count: int

        validate_model(MyModel)  # Raises if incompatible

    Args:
        model_class: Pydantic BaseModel class to validate

    Raises:
        OpenAIStrictModeValidationError: If schema violates strict mode requirements
    """
    schema = model_class.model_json_schema()
    validate_openai_strict_mode_schema(schema, model_class.__name__)


def validate_openai_strict_mode_schema(schema: dict[str, Any], model_name: str = "Model") -> None:
    """Validate that a JSON schema is OpenAI strict mode compatible.

    Checks for violations of OpenAI's strict mode requirements:
    - additionalProperties must be false for all objects
    - Disallowed format values (only date-time, email, uri, uuid, ipv4, ipv6 allowed)
    - uniqueItems (from set types) - not permitted
    - oneOf at root level - not permitted (anyOf is OK nested, but oneOf is never OK)
    - anyOf at root level of properties - not permitted
    - $ref with additional keywords (e.g., description alongside $ref)

    See: https://platform.openai.com/docs/guides/structured-outputs#supported-schemas

    Args:
        schema: JSON schema dict from model_json_schema()
        model_name: Model name for error messages

    Raises:
        OpenAIStrictModeValidationError: If schema violates strict mode requirements
    """
    errors: list[str] = []

    def check_recursive(obj: Any, path: str = "", is_property_root: bool = False, inside_defs: bool = False) -> None:
        """Recursively check schema object for violations.

        Args:
            obj: Schema object to check
            path: Current path for error messages
            is_property_root: True if this is the root level of a property definition
            inside_defs: True if we're inside a $defs entry (nested schema)
        """
        if not isinstance(obj, dict):
            return

        # Check for oneOf (never allowed in strict mode)
        if "oneOf" in obj:
            errors.append(f"{path}: oneOf is not supported in strict mode (use anyOf or flatten to single object)")

        # Note: anyOf is allowed at property level (e.g., properties.item.anyOf)
        # The only restriction is that the ROOT schema itself cannot be anyOf
        # See: https://platform.openai.com/docs/guides/structured-outputs/supported-schemas
        # Lines 341-356: "root level object must be an object, and not use anyOf"
        # Lines 458-516: Shows anyOf at property level is valid

        # Check for additionalProperties: false on objects with non-empty properties
        # Empty objects ({properties: {}}) don't need additionalProperties: false
        if "properties" in obj and obj["properties"] and obj.get("additionalProperties") is not False:
            errors.append(
                f"{path}: Objects must have additionalProperties: false "
                f"(use ConfigDict(extra='forbid') in Pydantic model)"
            )

        # Check that all properties are in required array
        # OpenAI strict mode: "required is required to be supplied and to be an array
        # including every key in properties"
        if "properties" in obj:
            property_keys = set(obj["properties"].keys())
            required_keys = set(obj.get("required", []))
            missing_required = property_keys - required_keys

            if missing_required:
                errors.append(
                    f"{path}: All properties must be in 'required' array. "
                    f"Missing: {sorted(missing_required)}. "
                    f"Use OpenAICompatibleSchema generator to auto-fix."
                )

        # Check for disallowed format values
        if "format" in obj:
            fmt = obj["format"]
            allowed_formats = {"date-time", "email", "uri", "uuid", "ipv4", "ipv6"}
            if fmt not in allowed_formats:
                errors.append(f"{path}: Disallowed format '{fmt}' (use str type instead)")

        # Check for uniqueItems (from set types)
        if obj.get("uniqueItems") is True:
            errors.append(f"{path}: uniqueItems not allowed (use list instead of set)")

        # Check for $ref with additional keywords (besides $ref itself)
        if "$ref" in obj:
            extra_keys = set(obj.keys()) - {"$ref"}
            if extra_keys:
                errors.append(f"{path}: $ref cannot have additional keywords {extra_keys}")

        # Recurse into nested structures
        if "properties" in obj:
            for prop_name, prop_schema in obj["properties"].items():
                # Only top-level properties (directly under Model.properties) are at "property root level"
                # Nested properties (e.g., Model.properties.config.properties.value) are NOT at property root
                # $defs creates a new schema context, so count properties from after the last $defs
                if ".$defs." in path:
                    # Schema inside $defs - count properties from after the defs entry
                    path_after_defs = path.split(".$defs.")[-1]
                    properties_count = path_after_defs.count(".properties.")
                else:
                    properties_count = path.count(".properties.")
                is_top_level_property = properties_count == 0  # No ".properties." yet means we're at the first level
                check_recursive(
                    prop_schema,
                    f"{path}.properties.{prop_name}",
                    is_property_root=is_top_level_property,
                    inside_defs=inside_defs,
                )

        if "items" in obj:
            check_recursive(obj["items"], f"{path}.items", inside_defs=inside_defs)

        if "$defs" in obj:
            for def_name, def_schema in obj["$defs"].items():
                # Schemas inside $defs are nested types - set inside_defs=True
                check_recursive(def_schema, f"{path}.$defs.{def_name}", inside_defs=True)

        if "anyOf" in obj and not is_property_root:  # anyOf is OK nested, just not at property root
            for i, variant in enumerate(obj["anyOf"]):
                check_recursive(variant, f"{path}.anyOf[{i}]", inside_defs=inside_defs)

        if "allOf" in obj:
            for i, variant in enumerate(obj["allOf"]):
                check_recursive(variant, f"{path}.allOf[{i}]", inside_defs=inside_defs)

    # Check that schema root is an object, not anyOf
    # Per OpenAI docs: "root level object of a schema must be an object, and not use anyOf"
    if "anyOf" in schema and "type" not in schema:
        errors.append(f"{model_name}: Schema root cannot use anyOf (must be an object with type: 'object')")

    check_recursive(schema, model_name)

    if errors:
        raise OpenAIStrictModeValidationError(
            f"OpenAI strict mode violations in {model_name}:\n" + "\n".join(f"  - {e}" for e in errors)
        )


class OpenAIStrictModeBaseModel(BaseModel):
    """Base class for OpenAI strict mode compatible Pydantic models.

    Use this as the base class for any model that will be used in MCP tool schemas
    exposed to OpenAI's API.

    **Automatic validation:** Schemas are automatically validated when the class is defined.
    If validation fails, an OpenAIStrictModeValidationError is raised at import time.
    This ensures all models using this base class are OpenAI strict mode compatible.

    OpenAI Strict Mode enforces schema adherence for function calling and structured outputs.
    See: https://platform.openai.com/docs/guides/function-calling (strict mode section)
         https://platform.openai.com/docs/guides/structured-outputs

    Key requirements (from OpenAI docs):
    1. additionalProperties must be false for all objects
    2. All fields must be required (use `| None` for optional fields)
    3. Format restrictions: only date-time, email, uri, uuid, ipv4, ipv6 allowed
       - No format="path" (use str instead of Path)
    4. No uniqueItems (use list instead of set)
    5. No oneOf (not supported - use anyOf or flatten to single object)
    6. No anyOf at property root level (flatten discriminated unions)
    7. No $ref with additional keywords (no Field(description=...) on type aliases)

    Additional limitations (see structured outputs docs):
    - Max 5000 object properties total, up to 10 levels of nesting
    - Max 1000 enum values across all enums
    - Total string length limits for property names, enum values, etc.

    Common patterns:
    - Use `str` instead of `Path` (no format="path")
    - Use `list[T]` instead of `set[T]` (no uniqueItems)
    - Use `field: Type | None = None` for optional fields (all fields must be in required)
    - Don't use Field(description=...) with type aliases that generate $ref
    - Use Field(ge=..., le=...) for numeric constraints (expressed in schema, not prose)
    - Flatten discriminated unions: instead of Union[A, B] with discriminator,
      use a single model with optional fields and a discriminator field

    Example:
        class MyToolInput(OpenAIStrictModeBaseModel):
            # str not Path: OpenAI strict mode doesn't accept format="path"
            cwd: str | None = None
            # list not set: OpenAI strict mode doesn't accept uniqueItems
            files: list[str]
            max_bytes: int = Field(ge=0, le=100_000)
    """

    model_config = ConfigDict(extra="forbid")

    @classmethod
    def __pydantic_init_subclass__(cls, **kwargs: Any) -> None:
        """Validate schema when Pydantic finishes initializing the subclass.

        This runs AFTER Pydantic's metaclass setup, so model_json_schema() works correctly.

        Automatically validates that the schema is OpenAI strict mode compatible.

        Raises:
            OpenAIStrictModeValidationError: If schema violates strict mode requirements
        """
        super().__pydantic_init_subclass__(**kwargs)
        # Always validate - this is what this base class is for
        cls.validate_openai_strict_mode()

    @classmethod
    def validate_openai_strict_mode(cls) -> None:
        """Validate that this model's JSON schema is OpenAI strict mode compatible.

        This is called automatically via __pydantic_init_subclass__ when the class is defined.
        Can also be called explicitly if needed.

        Uses OpenAICompatibleSchema generator to produce the schema that will actually
        be sent to OpenAI (with all fields in required, anyOf instead of oneOf, etc.).

        Raises:
            OpenAIStrictModeValidationError: If schema violates strict mode requirements
        """
        schema = cls.model_json_schema(schema_generator=OpenAICompatibleSchema)
        validate_openai_strict_mode_schema(schema, cls.__name__)
