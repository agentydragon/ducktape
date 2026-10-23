"""The `argument_schema` policy model and validation of its operator-authored JSON Schema."""

from __future__ import annotations

from typing import Literal

import jsonschema
from pydantic import Field, JsonValue, field_validator

from agentplane.action_service.models import PolicyKind
from agentplane.action_service.policies.kind import Kind

# types-jsonschema stubs import referencing; the mypy aspect needs that typed package directly.
# gazelle:include_dep @pypi//referencing


class ArgumentSchema(Kind):
    type: Literal[PolicyKind.ARGUMENT_SCHEMA]
    argument_schema: dict[str, JsonValue] = Field(
        alias="schema",
        description="A JSON Schema over the arguments object; `properties` alone never implies presence.",
    )

    @field_validator("argument_schema")
    @classmethod
    def _valid_schema(cls, value: dict[str, JsonValue]) -> dict[str, JsonValue]:
        try:
            jsonschema.validators.validator_for(value).check_schema(value)
        except jsonschema.SchemaError as error:
            raise ValueError(f"schema is not a valid JSON Schema: {error.message}") from error
        return value
