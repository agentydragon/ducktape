"""Focused contracts for Console runtime selection."""

from __future__ import annotations

from typing import Any, cast
from uuid import uuid4

import pytest
import pytest_bazel

from haku.console.harnesses.kind import HarnessKind
from haku.console.session.system_prompt import SystemPromptTemplate
from haku.console.x.claude_code import projection
from haku.console.x.claude_code.testing.fold import whole_capture
from haku.console.x.claude_code.testing.wire import assistant, recorded, text_block
from haku.console.x.runtime import (
    AgentRuntimeResources,
    RuntimeKey,
    RuntimeNotConfiguredError,
    RuntimeRegistry,
    UnsupportedRuntimeError,
)
from haku.console.x.runtime_catalog import projection_registry
from haku.runner.protocol import HarnessFrame


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


def test_runtime_adapter_keeps_claude_projection_behavior_unchanged() -> None:
    payload = assistant(text_block("hello"), message_id="msg_1")
    adapter = projection_registry()[HarnessKind.CLAUDE_CODE]

    through_adapter = adapter.turn_handler().apply(frame_seq=7, frame=HarnessFrame(frame=payload)).events
    through_native = (
        projection.ProjectionState()
        .advance([recorded(7, payload)], delta_source=projection.DeltaSource.STREAM_EVENTS)[1]
        .events
    )

    assert through_adapter == through_native


def test_claude_adapter_keeps_opaque_native_frames_inspectable() -> None:
    adapter = projection_registry()[HarnessKind.CLAUDE_CODE]
    payload = {"jsonrpc": "2.0", "method": "future/event", "params": {"opaque": True}}
    undiscriminated = {"jsonrpc": "2.0", "id": 1, "result": {}}

    effects = adapter.turn_handler().apply(frame_seq=8, frame=HarnessFrame(frame=payload))
    # The count is the reducer's rather than the handler's: `FrameEffects` carries a frame's
    # neutral effects, and a frame the adapter has no case for has none.
    counted = whole_capture([recorded(8, payload), recorded(9, undiscriminated)])

    assert effects.events == ()
    assert counted.events == ()
    assert counted.unprojected == {"future/event": 1, "<undiscriminated>": 1}


if __name__ == "__main__":
    pytest_bazel.main()
