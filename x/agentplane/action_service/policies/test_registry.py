"""The registry gate every kind shares: an Action a policy does not list never matches, whatever
the kind's own test would say; the union refuses an unknown `type` at parse."""

from __future__ import annotations

from collections.abc import Callable

import pytest
import pytest_bazel
from pydantic import TypeAdapter, ValidationError

from github_policy.visibility import RepositoryVisibilityService
from x.agentplane.action_service.catalog import ActionIdentity
from x.agentplane.action_service.policies.kind import Matched, NotMatched
from x.agentplane.action_service.policies.registry import Policy, evaluate

POLICIES = TypeAdapter(list[Policy])


async def test_unlisted_action_never_matches(github_visibility: Callable[..., RepositoryVisibilityService]) -> None:
    visibility = github_visibility(("test-owner", "test-repo"))
    policies = POLICIES.validate_python(
        [
            {"type": "exact_actions", "actions": {"github": ["get_file_contents"]}},
            {"type": "argument_schema", "actions": {"github": ["get_file_contents"]}, "schema": {}},
            {
                "type": "github_repository",
                "actions": {"github": ["get_file_contents"]},
                "owner": "test-owner",
                "repository": "test-repo",
            },
            {"type": "github_public_repository", "actions": {"github": ["get_file_contents"]}},
        ]
    )
    arguments = {"owner": "test-owner", "repo": "test-repo", "path": "README.md"}
    listed = ActionIdentity(group="github", name="get_file_contents")
    for policy in policies:
        assert isinstance(await evaluate(policy, listed, arguments, visibility), Matched)
    for unlisted in (
        ActionIdentity(group="github", name="issue_read"),
        ActionIdentity(group="other", name="get_file_contents"),
    ):
        for policy in policies:
            assert await evaluate(policy, unlisted, arguments, visibility) == NotMatched(
                f"{unlisted.group}/{unlisted.name} is not listed"
            )


def test_unknown_kind_is_refused() -> None:
    with pytest.raises(ValidationError):
        POLICIES.validate_python([{"type": "allow_all", "actions": {"everything": ["echo"]}}])


if __name__ == "__main__":
    pytest_bazel.main()
