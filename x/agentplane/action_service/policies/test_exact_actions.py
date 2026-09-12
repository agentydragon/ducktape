"""`exact_actions` matches by name alone; the name gate lives in the registry."""

from __future__ import annotations

from collections.abc import Callable

import pytest
import pytest_bazel

from github_policy.visibility import RepositoryVisibilityService
from x.agentplane.action_service.catalog import ActionIdentity
from x.agentplane.action_service.policies.exact_actions import ExactActions
from x.agentplane.action_service.policies.kind import Matched, NotMatched
from x.agentplane.action_service.policies.registry import evaluate

ECHO = ActionIdentity(group="everything", name="echo")
POLICY = ExactActions.model_validate({"type": "exact_actions", "actions": {"everything": ["echo"]}})


@pytest.mark.parametrize(
    ("action", "matched"),
    [
        (ECHO, True),
        (ActionIdentity(group="everything", name="add"), False),
        (ActionIdentity(group="other", name="echo"), False),
    ],
)
async def test_matches_by_name_alone(
    action: ActionIdentity, matched: bool, github_visibility: Callable[..., RepositoryVisibilityService]
) -> None:
    decision = await evaluate(POLICY, action, {"anything": [1, 2]}, github_visibility())
    assert isinstance(decision, Matched if matched else NotMatched)


if __name__ == "__main__":
    pytest_bazel.main()
