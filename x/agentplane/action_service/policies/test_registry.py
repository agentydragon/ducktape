"""The registry gate every kind shares: an Action a policy does not list never matches, whatever
the kind's own test would say; the union refuses an unknown `type` at parse."""

from __future__ import annotations

import pytest
import pytest_bazel
from pydantic import TypeAdapter, ValidationError

from x.agentplane.action_service.catalog import ActionIdentity
from x.agentplane.action_service.policies.kind import Matched, NotMatched
from x.agentplane.action_service.policies.registry import Policy, evaluate

POLICIES = TypeAdapter(list[Policy])


def test_unlisted_action_never_matches() -> None:
    exact, by_schema = POLICIES.validate_python(
        [
            {"type": "exact_actions", "actions": {"everything": ["echo"]}},
            {"type": "argument_schema", "actions": {"everything": ["echo"]}, "schema": {}},
        ]
    )
    listed = ActionIdentity(group="everything", name="echo")
    assert isinstance(evaluate(exact, listed, {}), Matched)
    assert isinstance(evaluate(by_schema, listed, {}), Matched)
    for unlisted in (ActionIdentity(group="everything", name="add"), ActionIdentity(group="other", name="echo")):
        not_listed = NotMatched(f"{unlisted.group}/{unlisted.name} is not listed")
        assert evaluate(exact, unlisted, {}) == not_listed
        assert evaluate(by_schema, unlisted, {}) == not_listed


def test_unknown_kind_is_refused() -> None:
    with pytest.raises(ValidationError):
        POLICIES.validate_python([{"type": "allow_all", "actions": {"everything": ["echo"]}}])


if __name__ == "__main__":
    pytest_bazel.main()
