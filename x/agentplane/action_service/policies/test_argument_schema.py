"""`argument_schema` has plain JSON Schema semantics, and refuses a schema that is not one."""

from __future__ import annotations

from collections.abc import Callable

import pytest
import pytest_bazel
from pydantic import JsonValue, ValidationError

from github_policy.visibility import RepositoryVisibilityService
from x.agentplane.action_service.catalog import ActionIdentity
from x.agentplane.action_service.policies.argument_schema import ArgumentSchema
from x.agentplane.action_service.policies.kind import Matched, NotMatched
from x.agentplane.action_service.policies.registry import evaluate

ECHO = ActionIdentity(group="everything", name="echo")
POLICY = ArgumentSchema.model_validate(
    {
        "type": "argument_schema",
        "actions": {"everything": ["echo"]},
        "schema": {
            "type": "object",
            "required": ["message"],
            "properties": {"message": {"type": "string", "maxLength": 5}, "count": {"type": "integer"}},
            "additionalProperties": False,
        },
    }
)


@pytest.mark.parametrize(
    ("arguments", "matched"),
    [
        ({"message": "hi"}, True),
        ({"message": "hi", "count": 2}, True),
        ({}, False),  # `required` decides presence; `properties` alone never does.
        ({"message": "too long"}, False),
        ({"message": "hi", "count": "2"}, False),
        ({"message": "hi", "extra": True}, False),
    ],
)
async def test_plain_json_schema_semantics(
    arguments: dict[str, JsonValue], matched: bool, github_visibility: Callable[..., RepositoryVisibilityService]
) -> None:
    decision = await evaluate(POLICY, ECHO, arguments, github_visibility())
    assert isinstance(decision, Matched if matched else NotMatched)


def test_invalid_json_schema_is_refused_at_parse() -> None:
    with pytest.raises(ValidationError, match="not a valid JSON Schema"):
        ArgumentSchema.model_validate(
            {"type": "argument_schema", "actions": {"everything": ["echo"]}, "schema": {"type": "nope"}}
        )


if __name__ == "__main__":
    pytest_bazel.main()
