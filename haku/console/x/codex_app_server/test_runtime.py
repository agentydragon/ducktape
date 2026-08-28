"""Codex as one concrete implementation of the neutral Console runtime seam."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest_bazel

from haku.console.conversation.conversation_event import FrameRange
from haku.console.harnesses.kind import HarnessKind
from haku.console.x.codex_app_server import projection
from haku.console.x.codex_app_server.client import CodexThread
from haku.console.x.codex_app_server.config import ReasoningEffort
from haku.console.x.codex_app_server.runtime import CodexRuntimeAdapter, CodexTurnHandler
from haku.console.x.conversation_events import ItemSegment, TurnAnswered, TurnCompleted, TurnFailed
from haku.console.x.runtime import OpenItemSeed, RuntimeLaunch, RuntimeMcpServer, TurnProjectionSeed
from haku.runner.client import FrameSink
from haku.runner.codex.options import CodexModelProvider
from haku.runner.protocol import HarnessFrame, TextWebSocket


def _launch(**overrides: Any) -> RuntimeLaunch:
    values: dict[str, Any] = {
        "cwd": "/workspace",
        "environment": {"CODEX_HOME": "/codex-home"},
        "mcp_servers": {
            "haku-console": RuntimeMcpServer(
                url="https://console.test/mcp", bearer_environment_variable="HAKU_AGENT_SDK_RUNNER_TOKEN"
            )
        },
        "appended_system_prompt": "you are Haku",
        "resume_from": 29,
    }
    values.update(overrides)
    return RuntimeLaunch(**values)


class CapturingFactory:
    def __init__(self) -> None:
        self.launch: Any = None
        self.thread: CodexThread | None = None
        self.result = object()

    def __call__(self, websocket, launch, progress, frames_to, thread):
        self.launch = launch
        self.thread = thread
        return self.result


def test_codex_builds_process_and_thread_configuration_from_the_same_neutral_launch() -> None:
    factory = CapturingFactory()
    adapter = CodexRuntimeAdapter(
        client_factory=factory,
        model="codex-gpt-5.6-sol",
        reasoning_effort=ReasoningEffort.LOW,
        model_provider=CodexModelProvider(
            provider_id="haku",
            name="Haku OpenAI-compatible",
            base_url="http://litellm.test/v1",
            api_key_env_var="OPENAI_API_KEY",
        ),
    )
    result = adapter.client(cast(TextWebSocket, object()), _launch(), None, cast(FrameSink, object()))

    assert result is factory.result
    assert factory.launch is not None
    assert factory.launch.arguments == (
        "-c",
        'model_provider = "haku"',
        "-c",
        'model_providers = {haku = {name = "Haku OpenAI-compatible", '
        'base_url = "http://litellm.test/v1", env_key = "OPENAI_API_KEY", '
        'wire_api = "responses"}}',
        "-c",
        'mcp_servers = {haku-console = {url = "https://console.test/mcp", '
        'bearer_token_env_var = "HAKU_AGENT_SDK_RUNNER_TOKEN"}}',
        "app-server",
        "--listen",
        "stdio://",
    )
    assert factory.launch.cwd == "/workspace"
    assert factory.launch.resume_from == 29
    assert factory.launch.environment == {"CODEX_HOME": "/codex-home"}
    assert factory.thread == CodexThread(
        cwd=Path("/workspace"),
        model="codex-gpt-5.6-sol",
        reasoning_effort=ReasoningEffort.LOW,
        developer_instructions="you are Haku",
    )


def test_codex_prompt_detection_reads_only_its_native_request_method() -> None:
    adapter = CodexRuntimeAdapter()

    assert adapter.prompt_submitted([HarnessFrame(frame={"method": "turn/start", "id": 3, "params": {}})])
    assert not adapter.prompt_submitted([HarnessFrame(frame={"method": "turn/started", "params": {}})])
    assert not adapter.prompt_submitted([HarnessFrame(frame={"type": "user"})])


def test_turn_handler_seeds_open_message_reasoning_and_call_completion_state() -> None:
    handler = cast(
        CodexTurnHandler,
        CodexRuntimeAdapter().turn_handler(
            TurnProjectionSeed(
                open_message=OpenItemSeed(text="half answer", first_frame_seq=4, last_frame_seq=6),
                open_reasoning=OpenItemSeed(text="half thought", first_frame_seq=7, last_frame_seq=8),
                seen_call_ids=frozenset({"call-open", "call-done"}),
                completed_call_ids=frozenset({"call-done"}),
            )
        ),
    )

    assert handler.state == projection.ProjectionState(
        open_message=projection.OpenItem(4, 6, None, "half answer"),
        open_reasoning=projection.OpenItem(7, 8, None, "half thought"),
        seen_call_ids=frozenset({"call-open", "call-done"}),
        completed_call_ids=frozenset({"call-done"}),
    )


def test_live_turn_handler_emits_neutral_events_and_terminal_completion() -> None:
    handler = CodexRuntimeAdapter().turn_handler()
    frames = [
        {"method": "item/started", "params": {"item": {"type": "agentMessage", "id": "message-1"}}},
        {"method": "item/agentMessage/delta", "params": {"itemId": "message-1", "delta": "hello"}},
        {"method": "item/completed", "params": {"item": {"type": "agentMessage", "id": "message-1", "text": "hello"}}},
        {"method": "turn/completed", "params": {"turn": {"id": "turn-1", "status": "completed"}}},
    ]
    effects = [handler.apply(frame_seq=index, frame=HarnessFrame(frame=frame)) for index, frame in enumerate(frames, 1)]

    assert any(isinstance(event, ItemSegment) for effect in effects for event in effect.events)
    assert effects[-1].events[-1] == TurnCompleted(end=TurnAnswered(), provenance=FrameRange(4, 4))
    assert effects[-1].completion is not None
    assert effects[-1].completion.end == TurnAnswered()
    assert effects[-1].completion.final_text == ""


def test_failed_native_turn_becomes_a_neutral_failure() -> None:
    effects = (
        CodexRuntimeAdapter()
        .turn_handler()
        .apply(
            frame_seq=9,
            frame=HarnessFrame(
                frame={
                    "method": "turn/completed",
                    "params": {"turn": {"id": "turn-1", "status": "failed", "error": {"message": "boom"}}},
                }
            ),
        )
    )

    assert effects.completion is not None
    assert effects.completion.end == TurnFailed(reason="boom")


def test_adapter_identity_is_codex_without_making_it_a_configured_runtime() -> None:
    adapter = CodexRuntimeAdapter()
    assert adapter.kind is HarnessKind.CODEX_APP_SERVER
    assert adapter.display_name == "Codex"


if __name__ == "__main__":
    pytest_bazel.main()
