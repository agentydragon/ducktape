"""`argument_schema` has plain JSON Schema semantics."""

from __future__ import annotations

from collections.abc import Callable

import pytest
import pytest_bazel
from pydantic import JsonValue

from agentplane.action_service.catalog import ActionIdentity
from agentplane.action_service.github_policy.visibility import RepositoryVisibilityService
from agentplane.action_service.policies.argument_schema import ArgumentSchema
from agentplane.action_service.policies.kind import Matched, NotMatched
from agentplane.action_service.policies.registry import evaluate

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


if __name__ == "__main__":
    pytest_bazel.main()
