"""ActionGroup/Action catalog: config-shaped validation, namespaced lookup, and view redaction."""

from __future__ import annotations

import textwrap

import pytest
import pytest_bazel
import yaml
from pydantic import ValidationError

from agentplane.action_service.catalog import ActionCatalog, McpExecutorBinding, UnknownActionError
from util.bazel.runfiles import get_required_path


def _catalog() -> ActionCatalog:
    # Mirrors `ActionCatalog(groups=settings.action_groups)` in main.async_main.
    return ActionCatalog(
        groups=yaml.safe_load(
            get_required_path("_main/agentplane/action_service/testdata/configured_action_groups.yaml").read_text()
        )
    )


def test_configured_groups_and_actions_are_discoverable() -> None:
    catalog = _catalog()

    assert isinstance(catalog.groups["github"].executor, McpExecutorBinding)
    views = {view.key: view for view in catalog.group_views(with_detail=False)}

    assert views.keys() == {"github", "calendar"}
    github = views["github"]
    assert github.title == "GitHub"
    assert github.available is True
    assert github.executor_kind == "mcp"
    assert github.executor_description == "Connected as Rai's GitHub account via the configured MCP server."
    assert [(action.group, action.name) for action in github.actions] == [("github", "get_file")]
    assert github.actions[0].description == "Read one file's contents from a public repository."
    assert github.actions[0].input_schema["required"] == ["owner", "repo", "path"]
    assert views["calendar"].available is False


def test_server_id_must_match_its_own_group_key() -> None:
    bad_yaml = textwrap.dedent("""
        github:
          title: GitHub
          description: x
          executor:
            kind: mcp
            description: x
            config: {server_id: not-github}
        """)

    with pytest.raises(ValidationError, match="not-github"):
        ActionCatalog(groups=yaml.safe_load(bad_yaml))


def test_a_direct_tool_group_key_cannot_hold_the_name_separator() -> None:
    group = yaml.safe_load(
        textwrap.dedent("""
            title: x
            description: x
            executor: {kind: mcp, description: x}
            """)
    )
    # The key is fine where no tool name is split at it.
    ActionCatalog(groups={"test__group": group})
    with pytest.raises(ValidationError, match="must not contain"):
        ActionCatalog.model_validate({"groups": {"test__group": {**group, "direct_tools": ["act"]}}})


@pytest.mark.parametrize(
    ("group_key", "action_key"), [("unknown-group", "get_file"), ("github", "unknown-action"), ("unknown", "unknown")]
)
def test_unknown_group_or_action_fails_clearly(group_key: str, action_key: str) -> None:
    catalog = _catalog()

    with pytest.raises(UnknownActionError) as excinfo:
        catalog.action_view(group_key, action_key)

    assert excinfo.value.group_key == group_key
    assert excinfo.value.action_key == action_key


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
        ActionCatalog(groups=yaml.safe_load(bad_yaml))


if __name__ == "__main__":
    pytest_bazel.main()
