"""ActionGroup/Action catalog: config-shaped validation, namespaced lookup, and view redaction."""

from __future__ import annotations

import textwrap

import pytest
import pytest_bazel
import yaml
from pydantic import ValidationError

from x.agentplane.action_service.catalog import ActionCatalog, UnknownActionError

# A reviewed runtime-configuration fixture: exactly the value of the `action_groups:` key in the
# YAML `main.Settings.AGENTPLANE_ACTIONS_CONFIG_FILE` names. Two groups: one available
# fixture-executor group proving discovery end to end, one deliberately unavailable group.
CONFIGURED_CATALOG_YAML = textwrap.dedent("""
    github:
      title: GitHub
      description: Read access to public GitHub repositories.
      executor:
        kind: mcp
        owner_summary: Connected as Rai's GitHub account via the configured MCP server.
        config:
          server_url: https://github-mcp.internal.example
          account_secret_ref: github-mcp-account
      actions:
        get_file:
          description: Read one file's contents from a public repository.
          input_schema:
            type: object
            properties:
              owner: {type: string}
              repo: {type: string}
              path: {type: string}
            required: [owner, repo, path]
    calendar:
      title: Calendar
      description: Read-only Google Calendar access, offline pending OAuth.
      available: false
      executor:
        kind: mcp
        owner_summary: Not yet connected.
      actions:
        list_events:
          description: List upcoming events.
""")


def _catalog() -> ActionCatalog:
    # Mirrors `ActionCatalog(groups=settings.action_groups)` in main.async_main.
    return ActionCatalog(groups=yaml.safe_load(CONFIGURED_CATALOG_YAML))


def test_configured_groups_and_actions_are_discoverable() -> None:
    catalog = _catalog()

    views = {view.key: view for view in catalog.group_views()}

    assert views.keys() == {"github", "calendar"}
    github = views["github"]
    assert github.title == "GitHub"
    assert github.available is True
    assert github.executor_kind == "mcp"
    assert github.owner_summary == "Connected as Rai's GitHub account via the configured MCP server."
    assert [action.id for action in github.actions] == ["github.get_file"]
    assert github.actions[0].description == "Read one file's contents from a public repository."
    assert github.actions[0].input_schema["required"] == ["owner", "repo", "path"]
    assert views["calendar"].available is False


def test_executor_backend_configuration_never_reaches_a_view() -> None:
    catalog = _catalog()

    rendered = "\n".join(view.model_dump_json() for view in catalog.group_views())

    assert "github-mcp-account" not in rendered
    assert "github-mcp.internal.example" not in rendered


def test_namespaced_action_lookup_resolves_the_configured_definition() -> None:
    catalog = _catalog()

    view = catalog.action_view("github", "get_file")

    assert view.id == "github.get_file"
    assert view.group == "github"
    assert view.name == "get_file"


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
        "github.public:\n  title: x\n  description: x\n  executor: {kind: mcp, owner_summary: x}\n",
        "Github:\n  title: x\n  description: x\n  executor: {kind: mcp, owner_summary: x}\n",
        "github:\n  title: x\n  description: x\n  executor: {kind: mcp, owner_summary: x}\n"
        "  actions:\n    Get-File:\n      description: x\n",
    ],
)
def test_group_and_action_keys_must_be_stable_identifiers(bad_yaml: str) -> None:
    with pytest.raises(ValidationError):
        ActionCatalog(groups=yaml.safe_load(bad_yaml))


if __name__ == "__main__":
    pytest_bazel.main()
