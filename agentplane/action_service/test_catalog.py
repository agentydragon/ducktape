"""ActionGroup configuration parsing and identifier validation."""

from __future__ import annotations

import pytest
import pytest_bazel
import yaml
from pydantic import TypeAdapter, ValidationError

from agentplane.action_service.catalog import ActionGroup, Key, McpExecutorBinding
from agentplane.action_service.session_delegation.binding import SessionDelegationExecutorBinding
from util.bazel.runfiles import get_required_path


def _configured_groups() -> dict[str, ActionGroup]:
    return TypeAdapter(dict[Key, ActionGroup]).validate_python(
        yaml.safe_load(
            get_required_path("_main/agentplane/action_service/testdata/configured_action_groups.yaml").read_text()
        )
    )


def test_configured_groups_and_actions_parse() -> None:
    groups = _configured_groups()

    assert groups.keys() == {"github", "calendar"}
    github = groups["github"]
    assert isinstance(github.executor, McpExecutorBinding)
    assert github.title == "GitHub"
    assert github.available is True
    assert github.executor.kind == "mcp"
    assert github.executor.description == "Connected as Rai's GitHub account via the configured MCP server."
    assert list(github.actions) == ["get_file"]
    assert github.actions["get_file"].description == "Read one file's contents from a public repository."
    assert github.actions["get_file"].input_schema["required"] == ["owner", "repo", "path"]
    assert groups["calendar"].available is False


@pytest.mark.parametrize(
    "bad_yaml",
    [
        "github.public:\n  title: x\n  description: x\n  executor: {kind: mcp, description: x}\n",
        "Github:\n  title: x\n  description: x\n  executor: {kind: mcp, description: x}\n",
        "github:\n  title: x\n  description: x\n  executor: {kind: mcp, description: x}\n"
        "  actions:\n    Get-File:\n      description: x\n",
    ],
)
def test_group_and_action_keys_must_be_stable_identifiers(bad_yaml: str) -> None:
    with pytest.raises(ValidationError):
        TypeAdapter(dict[Key, ActionGroup]).validate_python(yaml.safe_load(bad_yaml))


def test_session_delegation_executor_binding_is_accepted() -> None:
    group = ActionGroup.model_validate(
        {
            "title": "Sessions",
            "description": "Same-principal Session operations.",
            "executor": {"kind": "session_delegation", "description": "Forwards the verified caller."},
        }
    )

    assert isinstance(group.executor, SessionDelegationExecutorBinding)
    assert group.executor.kind == "session_delegation"


if __name__ == "__main__":
    pytest_bazel.main()
