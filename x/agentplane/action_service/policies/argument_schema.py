"""`argument_schema`: a listed Action matches when its arguments satisfy a JSON Schema, with plain
JSON Schema semantics: the policy says which properties are required and which may be absent, so
"absent or an integer" is expressible and `properties` alone never implies presence."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal

import jsonschema
from jsonschema.exceptions import best_match
from pydantic import Field, JsonValue, field_validator

from x.agentplane.action_service.catalog import ActionIdentity
from x.agentplane.action_service.models import PolicyKind
from x.agentplane.action_service.policies.kind import Kind, Matched, NotMatched

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


def evaluate(
    policy: ArgumentSchema, action: ActionIdentity, arguments: Mapping[str, JsonValue]
) -> Matched | NotMatched:
    validator = jsonschema.validators.validator_for(policy.argument_schema)(policy.argument_schema)
    error = best_match(validator.iter_errors(dict(arguments)))
    if error is None:
        return Matched(f"arguments of {action.group}/{action.name} satisfy the schema")
    return NotMatched(f"arguments do not satisfy the schema: {error.message}")
