from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from typing import cast

from openai import AsyncOpenAI
from openai.types.responses import FunctionTool, Response, ResponseFunctionToolCall, ResponseReasoningItem, Tool
from openai.types.responses.response_reasoning_item import Summary
import pytest

from ember.config import EnforcedSleepUntilUserMessagePolicy, OpenAISettings
from ember.history import ConversationHistory
from ember.matrix_client import ConversationStatus
from ember.openai_agent import OpenAIAgent
from ember.secrets import ProjectedSecret
import ember.tools.run_shell_command as run_shell_tool
from ember.tools.run_shell_command import ShellCommandResult
from ember.tools.sleep_until_user_message import (
    SleepUntilUserMessageArgs,
    build_spec as build_sleep_until_user_message_spec,
)


class FakeMatrixClient:
    def __init__(self, statuses: list[ConversationStatus] | None = None) -> None:
        self._statuses = statuses or [ConversationStatus()]

    async def get_conversation_status(self) -> ConversationStatus:
        if not self._statuses:
            return ConversationStatus()
        if len(self._statuses) == 1:
            return self._statuses[0]
        return self._statuses.pop(0)


@pytest.fixture
def settings(monkeypatch: pytest.MonkeyPatch) -> OpenAISettings:
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    return OpenAISettings(
        api_key_secret=ProjectedSecret(name="openai_api_key", env_var="OPENAI_API_KEY"),
        model="gpt-5",
        system_prompt="system prompt",
    )


@pytest.fixture
def history(tmp_path: Path) -> ConversationHistory:
    return ConversationHistory(tmp_path / "history.jsonl")


@pytest.fixture
def matrix_client() -> FakeMatrixClient:
    return FakeMatrixClient()


def _make_openai_client(api_key: str, responses: list[Response]) -> AsyncOpenAI:
    client = AsyncOpenAI(api_key=api_key)

    async def _create(**kwargs):  # type: ignore[no-untyped-def]
        if responses:
            return responses.pop(0)
        raise RuntimeError("Unexpected additional model request")

    client.responses.create = _create
    return client


@pytest.fixture
def agent_factory(settings: OpenAISettings, history: ConversationHistory, matrix_client: FakeMatrixClient):
    workspace_path = history.path.parent  # type: ignore[attr-defined]

    def factory(client: AsyncOpenAI) -> OpenAIAgent:
        return OpenAIAgent(settings, history, client, matrix_client, workspace_path, None)

    return factory


@pytest.mark.asyncio
async def test_agent_runs_shell_command(
    monkeypatch: pytest.MonkeyPatch, settings: OpenAISettings, history: ConversationHistory, agent_factory
) -> None:
    client = _make_openai_client(
        settings.api_key_secret.value(required=True),
        [
            _response_with_tool_call(
                call_id="call-1", tool_name="run_shell_command", arguments='{"command": "echo hi"}'
            ),
            _response_with_tool_call(call_id="call-2", tool_name="sleep_until_user_message", arguments="{}"),
        ],
    )

    async def fake_run_command(command: str) -> ShellCommandResult:
        return ShellCommandResult(exit_code=0, stdout=f"ran {command}", stderr="")

    monkeypatch.setattr(run_shell_tool, "_run_command", fake_run_command)

    agent = agent_factory(client)
    await agent.handle_user_message("incoming message")

    assert agent.waiting_for_matrix

    items = [cast(Mapping[str, object], item) for item in history.build_input_items(settings.system_prompt)]
    reasoning_items = [item for item in items if item.get("type") == "reasoning"]
    assert reasoning_items
    reasoning_model = ResponseReasoningItem.model_validate(reasoning_items[0])
    assert reasoning_model.encrypted_content == "ciphertext"
    assert reasoning_model.content in (None, [])
    outputs = [item for item in items if item.get("type") == "function_call_output"]
    assert outputs
    run_command_output = next(
        payload for payload in (json.loads(cast(str, item["output"])) for item in outputs) if "exit_code" in payload
    )
    assert run_command_output == {"exit_code": 0, "stdout": "ran echo hi", "stderr": "", "timed_out": False}

    await client.close()


@pytest.mark.asyncio
async def test_agent_sleep_until_user_message(
    settings: OpenAISettings, history: ConversationHistory, agent_factory
) -> None:
    client = _make_openai_client(
        settings.api_key_secret.value(required=True),
        [_response_with_tool_call(call_id="call-sleep", tool_name="sleep_until_user_message", arguments="{}")],
    )

    agent = agent_factory(client)
    await agent.handle_user_message("ready to idle")

    assert agent.waiting_for_matrix

    items = [cast(Mapping[str, object], entry) for entry in history.build_input_items(settings.system_prompt)]
    reasoning_items = [item for item in items if item.get("type") == "reasoning"]
    assert reasoning_items
    reasoning_model = ResponseReasoningItem.model_validate(reasoning_items[0])
    assert reasoning_model.encrypted_content == "ciphertext"
    assert reasoning_model.content in (None, [])
    function_outputs = [item for item in items if item.get("type") == "function_call_output"]
    assert function_outputs
    sleep_payload = cast(str, function_outputs[-1]["output"])
    assert json.loads(sleep_payload) == {"status": "waiting_for_matrix", "reason": None}

    await client.close()


def _response_with_tool_call(call_id: str, tool_name: str, arguments: str) -> Response:
    reasoning = ResponseReasoningItem(
        id=f"reasoning-{call_id}",
        type="reasoning",
        summary=[Summary(type="summary_text", text="thinking")],
        content=None,
        encrypted_content="ciphertext",
    )

    function_call = ResponseFunctionToolCall(call_id=call_id, name=tool_name, arguments=arguments, type="function_call")

    tools = cast(
        list[Tool],
        [
            FunctionTool(
                name="run_shell_command",
                description="Execute shell command.",
                parameters={"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]},
                strict=False,
                type="function",
            ),
            FunctionTool(
                name="sleep_until_user_message",
                description="Sleep until user message arrives.",
                parameters={"type": "object", "properties": {}},
                strict=False,
                type="function",
            ),
        ],
    )

    return Response(
        id=f"resp-{call_id}",
        created_at=0.0,
        model="gpt-5",
        object="response",
        output=[reasoning, function_call],
        parallel_tool_calls=False,
        tool_choice="required",
        tools=tools,
    )


@pytest.mark.asyncio
async def test_sleep_until_user_message_rejected_when_enforced_policy_blocks() -> None:
    policy = EnforcedSleepUntilUserMessagePolicy(timeout_seconds=30)
    now = datetime.now(timezone.utc)
    status = ConversationStatus(last_user_message_at=now, last_agent_message_at=now - timedelta(seconds=60))

    class Provider:
        async def get_conversation_status(self) -> ConversationStatus:
            return status

    sleep_called = False

    def _mark_sleep() -> None:
        nonlocal sleep_called
        sleep_called = True

    spec = build_sleep_until_user_message_spec(_mark_sleep, Provider(), policy)
    result = await spec.handler(SleepUntilUserMessageArgs())

    assert result.status == "rejected"
    assert result.reason
    assert not sleep_called
