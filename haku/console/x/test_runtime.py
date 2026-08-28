"""Focused contracts for Console runtime selection."""

from __future__ import annotations

from typing import Any, cast
from uuid import uuid4

import pytest
import pytest_bazel

from haku.console.harnesses.kind import HarnessKind
from haku.console.session.system_prompt import SystemPromptTemplate
from haku.console.x.runtime import (
    AgentRuntimeResources,
    RuntimeKey,
    RuntimeNotConfiguredError,
    RuntimeRegistry,
    UnsupportedRuntimeError,
)
from haku.console.x.runtime_catalog import projection_registry


def test_projection_registry_exposes_each_linked_provider_adapter() -> None:
    registry = projection_registry()

    assert registry.kinds == frozenset({HarnessKind.CLAUDE_CODE, HarnessKind.CODEX_APP_SERVER})
    assert registry[HarnessKind.CLAUDE_CODE].kind is HarnessKind.CLAUDE_CODE
    assert registry[HarnessKind.CODEX_APP_SERVER].kind is HarnessKind.CODEX_APP_SERVER


def test_registry_fails_closed_for_a_runtime_kind_that_is_not_registered() -> None:
    registry = projection_registry()

    with pytest.raises(UnsupportedRuntimeError, match="not registered"):
        registry[cast(HarnessKind, "future_runtime")]


def test_execution_resources_are_selected_by_agent_runtime_and_pinned_profile() -> None:
    adapter = projection_registry()[HarnessKind.CLAUDE_CODE]
    first_agent = uuid4()
    second_agent = uuid4()

    def resource(agent_id, profile, cwd):
        return AgentRuntimeResources(
            claims=cast(Any, object()),
            session_ttl_seconds=300,
            cwd=cwd,
            environment={},
            mcp_server_urls={},
            system_prompt=SystemPromptTemplate(""),
            agent_id=agent_id,
            access_profile_id=profile,
        )

    registry = RuntimeRegistry(
        {HarnessKind.CLAUDE_CODE: adapter},
        {
            RuntimeKey(first_agent, HarnessKind.CLAUDE_CODE): resource(first_agent, "haku", "/haku"),
            RuntimeKey(second_agent, HarnessKind.CLAUDE_CODE): resource(second_agent, "coder", "/coder"),
        },
    )

    assert registry.configured(first_agent, HarnessKind.CLAUDE_CODE, access_profile_id="haku").resources.cwd == "/haku"
    assert (
        registry.configured_for(
            RuntimeKey(second_agent, HarnessKind.CLAUDE_CODE), access_profile_id="coder"
        ).resources.cwd
        == "/coder"
    )
    with pytest.raises(RuntimeNotConfiguredError, match="access profile"):
        registry.configured(first_agent, HarnessKind.CLAUDE_CODE, access_profile_id="coder")


if __name__ == "__main__":
    pytest_bazel.main()
