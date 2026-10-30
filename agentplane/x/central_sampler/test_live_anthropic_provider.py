"""Direct Anthropic Messages acceptance against real Postgres, V8 and executor fixtures.

Run explicitly with ANTHROPIC_API_KEY available to the test action:
`bbr test //agentplane/x/central_sampler:test_live_anthropic_provider.live --config=live-openai
--test_tag_filters=live_openai_api`.
"""

import asyncio
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import pytest_bazel
from pydantic import SecretStr

from agentplane.proto import event_pb2
from agentplane.runner import runner_service_pb2
from agentplane.x.central_sampler import sampler_pb2, sampler_pb2_grpc
from agentplane.x.central_sampler.settings import (
    AnthropicBlockBindingConfig,
    AnthropicCompactionConfig,
    AnthropicMessagesConfig,
    AnthropicPromptCacheConfig,
    AnthropicThinkingConfig,
    AuthConfig,
    ConnectionConfig,
    ModelConfig,
    Settings,
)
from agentplane.x.central_sampler.testing import METADATA, open_request, service, settings
from agentplane.x.central_sampler.testing_store import IntentSnapshot, Store
from util.testing.sqlalchemy_engines import postgres_async_engine
from util.testing.undeclared_outputs import undeclared_outputs_dir

TURN_TIMEOUT_SECONDS = 150
ROUTE = "anthropic-live"
DEFAULT_MODEL = "claude-haiku-5-5"
BASE_URL = "https://api.anthropic.com"


@dataclass
class Turn:
    completed: event_pb2.TurnCompleted
    assistant_text: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class SessionState:
    blocked: bool
    intents: list[IntentSnapshot]


async def follow_turn(stub: sampler_pb2_grpc.SamplerAsyncStub, session_id: str, turn_id: str) -> Turn:
    texts: dict[str, str] = {}
    assistant_items: set[str] = set()
    active = False
    stream = stub.FollowEvents(sampler_pb2.FollowEventsRequest(session_id=session_id, follow=True), metadata=METADATA)
    try:
        async with asyncio.timeout(TURN_TIMEOUT_SECONDS + 30):
            async for frame in stream:
                event = frame.entry.event
                if event.HasField("turn_started"):
                    active = event.turn_started.turn_id == turn_id
                if not active:
                    continue
                if event.HasField("item_started") and event.item_started.kind == event_pb2.ITEM_KIND_ASSISTANT_TEXT:
                    assistant_items.add(event.item_started.item_id)
                elif event.HasField("item_completed") and event.item_completed.HasField("text"):
                    texts[event.item_completed.item_id] = event.item_completed.text
                elif event.HasField("turn_completed") and event.turn_completed.turn_id == turn_id:
                    return Turn(
                        completed=event.turn_completed,
                        assistant_text=[text for item, text in texts.items() if item in assistant_items],
                    )
    finally:
        stream.cancel()
    raise AssertionError("event stream ended before the turn completed")


async def submit(stub: sampler_pb2_grpc.SamplerAsyncStub, session_id: str, command_id: str, text: str) -> Turn:
    await stub.SubmitInput(
        runner_service_pb2.SubmitInputRequest(session_id=session_id, command_id=command_id, text=text),
        metadata=METADATA,
        timeout=10,
    )
    return await follow_turn(stub, session_id, command_id)


async def wait_applied(stub: sampler_pb2_grpc.SamplerAsyncStub, session_id: str, command_id: str) -> None:
    async with asyncio.timeout(TURN_TIMEOUT_SECONDS):
        while True:
            result = await stub.GetCommand(
                sampler_pb2.GetCommandRequest(session_id=session_id, command_id=command_id),
                metadata=METADATA,
                timeout=10,
            )
            if result.outcome == sampler_pb2.COMMAND_OUTCOME_APPLIED:
                return
            if result.outcome in {sampler_pb2.COMMAND_OUTCOME_FAILED, sampler_pb2.COMMAND_OUTCOME_UNKNOWN}:
                raise AssertionError(f"command {command_id} ended with {result.outcome}: {result.failure_reason}")
            await asyncio.sleep(0.2)


def anthropic_settings(
    base: Settings,
    model: str,
    *,
    max_tokens: int,
    compaction: AnthropicCompactionConfig | None = None,
    prompt_cache: bool = False,
    per_message_effort: bool = False,
    cache_diagnostics: bool = False,
) -> Settings:
    provider_config = AnthropicMessagesConfig(
        max_tokens=max_tokens,
        thinking=AnthropicThinkingConfig(
            type="adaptive", block_binding=AnthropicBlockBindingConfig(prefix_mismatch_behavior="error")
        ),
        prompt_cache=AnthropicPromptCacheConfig(ttl="5m") if prompt_cache else None,
        cache_diagnostics=prompt_cache or cache_diagnostics,
        per_message_effort=per_message_effort,
        strict_tool_input=True,
        eager_input_streaming=True,
        compaction=compaction,
    )
    return base.model_copy(
        update={
            "connections": {
                "anthropic": ConnectionConfig(
                    base_url=BASE_URL,
                    auth=AuthConfig(type="anthropic_api_key", credential=SecretStr(os.environ["ANTHROPIC_API_KEY"])),
                )
            },
            "models": {
                ROUTE: ModelConfig(
                    connection="anthropic",
                    provider_model=model,
                    api="anthropic_messages",
                    exec_encoding="json",
                    reasoning_efforts={"low", "high"},
                    anthropic_messages=provider_config,
                )
            },
        }
    )


def body_of(intent: IntentSnapshot) -> dict[str, Any]:
    body = intent.request.get("body")
    assert isinstance(body, dict), "provider dispatch did not retain its structured request body"
    wire = intent.request.get("wire_request")
    assert isinstance(wire, str), "provider dispatch did not retain the serialized wire request"
    assert json.loads(wire) == body, "stored wire request differs from the structured request"
    return body


def response_of(intent: IntentSnapshot) -> dict[str, Any]:
    assert intent.result is not None, f"provider intent {intent.id} has no result"
    response = intent.result.get("response")
    assert isinstance(response, dict), f"provider intent {intent.id} did not retain its native response"
    assert intent.result.get("http_status") == 200
    assert isinstance(intent.result.get("provider_request_id"), str)
    events = intent.result.get("events")
    assert isinstance(events, list)
    assert events
    return response


def sampling_intents(intents: list[IntentSnapshot]) -> list[IntentSnapshot]:
    return [intent for intent in intents if intent.request.get("kind") == "sampling"]


def request_for_user_marker(intents: list[IntentSnapshot], marker: str) -> IntentSnapshot:
    matches = [intent for intent in sampling_intents(intents) if last_user_content_contains(body_of(intent), marker)]
    assert matches, f"no stored sampling request contains the user marker {marker}"
    return min(matches, key=lambda intent: len(body_of(intent).get("messages", [])))


def last_user_content_contains(body: dict[str, Any], marker: str) -> bool:
    messages = body.get("messages", [])
    if not isinstance(messages, list):
        return False
    users = [message for message in messages if isinstance(message, dict) and message.get("role") == "user"]
    return bool(users and marker in json.dumps(users[-1].get("content"), sort_keys=True))


def content_blocks(message: dict[str, Any]) -> list[dict[str, Any]]:
    content = message.get("content")
    if not isinstance(content, list):
        return []
    return [block for block in content if isinstance(block, dict)]


def has_block(messages: object, expected: dict[str, Any]) -> bool:
    if not isinstance(messages, list):
        return False
    return any(
        expected == block for message in messages if isinstance(message, dict) for block in content_blocks(message)
    )


def cache_count(intent: IntentSnapshot, field: str) -> int:
    usage = response_of(intent).get("usage")
    assert isinstance(usage, dict), "provider omitted usage; cache diagnostics are unavailable"
    assert field in usage, f"provider omitted {field}; cache status is unavailable"
    count = usage[field]
    assert isinstance(count, int), f"provider returned an invalid {field} cache diagnostic"
    assert count > 0, f"provider reported {field}=0, which is a cache miss"
    return count


def blocks_with_type(response: dict[str, Any], kind: str) -> list[dict[str, Any]]:
    content = response.get("content")
    if not isinstance(content, list):
        return []
    return [block for block in content if isinstance(block, dict) and block.get("type") == kind]


@pytest.mark.live_openai_api(credential_env="ANTHROPIC_API_KEY")
async def test_live_anthropic_exec_compaction_restart_and_signed_thinking_replay(
    database_url: str, execution_target: str, codex_binary: str, code_mode_host: str, tmp_path: Path
) -> None:
    model = os.environ.get("ANTHROPIC_MODEL", DEFAULT_MODEL)
    base = settings(
        database_url, "https://unused.invalid", execution_target, codex_binary, code_mode_host, undeclared_outputs_dir()
    )
    config = anthropic_settings(
        base,
        model,
        max_tokens=4096,
        compaction=AnthropicCompactionConfig(max_tokens=2048, keep_recent_turns=1),
        per_message_effort=True,
        cache_diagnostics=True,
    ).model_copy(update={"turn_timeout_seconds": TURN_TIMEOUT_SECONDS})
    request = open_request(tmp_path)
    request.spec.model = ROUTE
    request.spec.reasoning_effort = "low"
    token = f"sampler-anthropic-{uuid4().hex}"
    fact_token = uuid4().hex
    fact = f"the vault word is {fact_token}"
    first_marker = f"ANTHROPIC_FIRST_{uuid4().hex}"
    second_marker = f"ANTHROPIC_SECOND_{uuid4().hex}"
    third_marker = f"ANTHROPIC_THIRD_{uuid4().hex}"
    resumed_marker = f"ANTHROPIC_RESUMED_{uuid4().hex}"

    async with service(config) as stub:
        await stub.OpenSession(request, metadata=METADATA, timeout=10)
        first = await submit(
            stub,
            request.session_id,
            "anthropic-first",
            f"{first_marker}: use the exec tool exactly once to call exec_command with a shell command that prints "
            f"{token}; then report the exact printed token.",
        )
        assert first.completed.status == event_pb2.TURN_STATUS_COMPLETED, first.completed.error
        assert any(token in text for text in first.assistant_text), first.assistant_text
        await stub.ChangeReasoningEffort(
            runner_service_pb2.ChangeReasoningEffortRequest(
                session_id=request.session_id, command_id="anthropic-effort-change", effort="high"
            ),
            metadata=METADATA,
            timeout=10,
        )
        await wait_applied(stub, request.session_id, "anthropic-effort-change")
        second = await submit(
            stub,
            request.session_id,
            "anthropic-second",
            f"{second_marker}: remember this explicit fact for the next turn: {fact}. Reply with the exact fact.",
        )
        assert second.completed.status == event_pb2.TURN_STATUS_COMPLETED, second.completed.error
        assert any(fact_token in text for text in second.assistant_text), second.assistant_text
        third = await submit(
            stub,
            request.session_id,
            "anthropic-third",
            f"{third_marker}: Testing starts March 3, 2026 (count that date as day 1), takes 10 weekdays, and pauses on March 9 and March 16. What date does it end? "
            "Return the ISO date and the exact vault word.",
        )
        assert third.completed.status == event_pb2.TURN_STATUS_COMPLETED, third.completed.error
        assert any(fact_token in text for text in third.assistant_text), third.assistant_text

        precompact = await sampler_session(request.session_id, database_url)
        assert not precompact.blocked
        first_request = request_for_user_marker(precompact.intents, first_marker)
        first_body = body_of(first_request)
        assert first_body["stream"] is True
        exec_tools = [tool for tool in first_body.get("tools", []) if tool.get("name") == "exec"]
        assert len(exec_tools) == 1
        assert exec_tools[0].get("strict") is True, "Anthropic exec tool is not strict JSON"
        assert exec_tools[0].get("input_schema", {}).get("type") == "object"

        first_response = response_of(first_request)
        exec_calls = [block for block in blocks_with_type(first_response, "tool_use") if block.get("name") == "exec"]
        assert exec_calls, "model did not call the configured JSON exec tool"
        call = exec_calls[0]
        assert isinstance(call.get("input"), dict)
        assert isinstance(call["input"].get("code"), str)
        assert first_request.result is not None
        first_events = first_request.result["events"]
        partials = [
            event.get("delta", {}).get("partial_json", "")
            for event in first_events
            if isinstance(event, dict)
            and event.get("type") == "content_block_delta"
            and event.get("delta", {}).get("type") == "input_json_delta"
        ]
        assert partials, "provider SSE did not retain streamed JSON argument deltas"
        assert json.loads("".join(partials)) == call["input"]
        partial_event_indexes = [
            index
            for index, event in enumerate(first_events)
            if isinstance(event, dict)
            and event.get("type") == "content_block_delta"
            and event.get("delta", {}).get("type") == "input_json_delta"
        ]
        block_stop_indexes = [
            index
            for index, event in enumerate(first_events)
            if isinstance(event, dict) and event.get("type") == "content_block_stop"
        ]
        message_stop_indexes = [
            index
            for index, event in enumerate(first_events)
            if isinstance(event, dict) and event.get("type") == "message_stop"
        ]
        assert block_stop_indexes
        assert message_stop_indexes
        assert max(partial_event_indexes) < max(block_stop_indexes)
        assert max(block_stop_indexes) < max(message_stop_indexes)
        assert first_response.get("stop_reason") == "tool_use"

        later_bodies = [
            body_of(intent) for intent in sampling_intents(precompact.intents) if intent.id != first_request.id
        ]
        assert any(
            any(
                block.get("type") == "tool_result" and token in json.dumps(block)
                for message in body.get("messages", [])
                if isinstance(message, dict)
                for block in content_blocks(message)
            )
            for body in later_bodies
        ), "full tool result was not present in the following provider request"

        second_request = request_for_user_marker(precompact.intents, second_marker)
        third_request = request_for_user_marker(precompact.intents, third_marker)
        assert body_of(second_request).get("output_config", {}).get("effort") == "low"
        assert body_of(third_request).get("output_config", {}).get("effort") == "low"
        assert body_of(first_request).get("diagnostics", {}).get("previous_message_id") is None
        thinking_blocks = [
            block
            for block in blocks_with_type(response_of(third_request), "thinking")
            if isinstance(block.get("signature"), str) and block["signature"]
        ]
        assert thinking_blocks, "adaptive thinking did not produce a signed thinking block in the kept turn"
        retained_thinking = thinking_blocks[-1]

        await stub.Compact(
            sampler_pb2.CompactRequest(session_id=request.session_id, command_id="anthropic-compact"),
            metadata=METADATA,
            timeout=10,
        )
        await wait_applied(stub, request.session_id, "anthropic-compact")
        compacted = await sampler_session(request.session_id, database_url)
        compact_intents = [intent for intent in compacted.intents if intent.request.get("kind") == "compaction"]
        assert compact_intents, "manual compaction did not retain its provider request/result"
        compact_intent = compact_intents[-1]
        compact_body = body_of(compact_intent)
        compact_response = response_of(compact_intent)
        assert compact_body.get("output_config", {}).get("effort") == "low"
        assert any(
            isinstance(message, dict) and message.get("output_config", {}).get("effort") == "high"
            for message in compact_body.get("messages", [])
        ), "the effort change inside the summarized prefix was dropped"
        assert second_marker in json.dumps(compact_body.get("messages", []), sort_keys=True)
        assert third_marker not in json.dumps(compact_body.get("messages", []), sort_keys=True)
        compact_blocks = blocks_with_type(compact_response, "compaction")
        assert len(compact_blocks) == 1
        compaction_block = compact_blocks[0]
        assert compact_response.get("content") == [compaction_block]
        assert isinstance(compaction_block.get("signature"), str)
        assert compaction_block["signature"]
        assert compact_response.get("stop_reason") == "compaction"
        assert not compacted.blocked

    async with service(config) as stub:
        await stub.SubmitInput(
            runner_service_pb2.SubmitInputRequest(
                session_id=request.session_id,
                command_id="anthropic-resume",
                text=f"{resumed_marker}: what was the exact vault word I asked you to retain? Then use exec exactly "
                f"once to run exec_command and print {token} again.",
            ),
            metadata=METADATA,
            timeout=10,
        )
        resumed = await follow_turn(stub, request.session_id, "anthropic-resume")
        state = await sampler_session(request.session_id, database_url)

    assert resumed.completed.status == event_pb2.TURN_STATUS_COMPLETED, resumed.completed.error
    assert any(fact_token in text for text in resumed.assistant_text), resumed.assistant_text
    assert any(token in text for text in resumed.assistant_text), resumed.assistant_text
    assert not state.blocked
    resumed_requests = [
        intent
        for intent in sampling_intents(state.intents)
        if resumed_marker in json.dumps(body_of(intent).get("messages", []), sort_keys=True)
    ]
    assert resumed_requests, "restart continuation did not send a provider request"
    resumed_requests.sort(key=lambda intent: len(body_of(intent).get("messages", [])))
    assert any(has_block(body_of(intent).get("messages"), retained_thinking) for intent in resumed_requests), (
        "compaction or restart changed the retained signed thinking block"
    )
    previous_message_id = response_of(third_request).get("id")
    for intent in resumed_requests:
        resumed_body = body_of(intent)
        assert resumed_body["messages"][0] == {"role": "assistant", "content": [compaction_block]}
        assert resumed_body["messages"][1]["role"] == "user", "a control message split compaction from the kept turn"
        assert third_marker in json.dumps(resumed_body["messages"][1].get("content"), sort_keys=True)
        assert resumed_body.get("output_config", {}).get("effort") == "low"
        assert resumed_body.get("diagnostics", {}).get("previous_message_id") == previous_message_id
        assert response_of(intent).get("input_transformations") == [], (
            "provider dropped a preserved thinking block during continuation"
        )
        previous_message_id = response_of(intent).get("id")
        messages = resumed_body["messages"]
        resumed_user_index = next(
            index
            for index, message in enumerate(messages)
            if isinstance(message, dict)
            and message.get("role") == "user"
            and resumed_marker in json.dumps(message.get("content"), sort_keys=True)
        )
        restore_controls = [
            index
            for index, message in enumerate(messages)
            if isinstance(message, dict)
            and message.get("role") == "system"
            and message.get("content") == []
            and message.get("output_config", {}).get("effort") == "high"
        ]
        assert restore_controls, "compaction effort restoration control was not sent"
        assert min(restore_controls) > resumed_user_index, (
            "compaction effort restoration must be emitted after the first new user message"
        )

    resumed_calls = [
        intent
        for intent in state.intents
        if intent.request.get("kind") == "tool"
        and "exec_command" in json.dumps(intent.request.get("tool_name"))
        and intent.result is not None
    ]
    assert any(token in json.dumps(intent.result) for intent in resumed_calls), "resumed tool result was not journaled"


@pytest.mark.live_openai_api(credential_env="ANTHROPIC_API_KEY")
async def test_live_anthropic_prompt_cache_survives_effort_change(
    database_url: str, execution_target: str, codex_binary: str, code_mode_host: str, tmp_path: Path
) -> None:
    model = os.environ.get("ANTHROPIC_MODEL", DEFAULT_MODEL)
    base = settings(
        database_url, "https://unused.invalid", execution_target, codex_binary, code_mode_host, undeclared_outputs_dir()
    )
    config = anthropic_settings(base, model, max_tokens=2048, prompt_cache=True, per_message_effort=True).model_copy(
        update={"turn_timeout_seconds": TURN_TIMEOUT_SECONDS}
    )
    request = open_request(tmp_path)
    request.spec.model = ROUTE
    request.spec.reasoning_effort = "low"
    stable_prefix = "copper " * 3072 + "Stable cache prefix end."
    request.spec.instructions = stable_prefix
    markers = [f"ANTHROPIC_CACHE_TURN_{index}_{uuid4().hex}" for index in range(1, 4)]

    async with service(config) as stub:
        await stub.OpenSession(request, metadata=METADATA, timeout=10)
        first = await submit(stub, request.session_id, "cache-first", f"{markers[0]}: reply READY.")
        second = await submit(stub, request.session_id, "cache-second", f"{markers[1]}: reply READY again.")
        assert first.completed.status == event_pb2.TURN_STATUS_COMPLETED, first.completed.error
        assert second.completed.status == event_pb2.TURN_STATUS_COMPLETED, second.completed.error

        await stub.ChangeReasoningEffort(
            runner_service_pb2.ChangeReasoningEffortRequest(
                session_id=request.session_id, command_id="cache-effort-change", effort="high"
            ),
            metadata=METADATA,
            timeout=10,
        )
        await wait_applied(stub, request.session_id, "cache-effort-change")
        third = await submit(stub, request.session_id, "cache-third", f"{markers[2]}: reply READY once more.")
        assert third.completed.status == event_pb2.TURN_STATUS_COMPLETED, third.completed.error
        state = await sampler_session(request.session_id, database_url)

    assert not state.blocked
    first_request = request_for_user_marker(state.intents, markers[0])
    second_request = request_for_user_marker(state.intents, markers[1])
    third_request = request_for_user_marker(state.intents, markers[2])
    first_body, second_body, third_body = map(body_of, (first_request, second_request, third_request))
    for body in (first_body, second_body, third_body):
        assert stable_prefix in json.dumps(body.get("system"), sort_keys=True)
        assert body.get("output_config", {}).get("effort") == "low", "top-level launch effort changed"

    assert first_body.get("diagnostics", {}).get("previous_message_id") is None
    assert second_body.get("diagnostics", {}).get("previous_message_id") == response_of(first_request).get("id")
    assert third_body.get("diagnostics", {}).get("previous_message_id") == response_of(second_request).get("id")

    cache_count(first_request, "cache_creation_input_tokens")
    cache_count(second_request, "cache_read_input_tokens")
    cache_count(third_request, "cache_read_input_tokens")
    effort_controls = [
        (index, message)
        for index, message in enumerate(third_body.get("messages", []))
        if isinstance(message, dict)
        and message.get("role") == "system"
        and message.get("content") == []
        and message.get("output_config", {}).get("effort") == "high"
    ]
    assert effort_controls, "effort change was not appended after the cached prefix as a system control"
    assert len(effort_controls) == 1
    latest_user = max(
        index
        for index, message in enumerate(third_body.get("messages", []))
        if isinstance(message, dict) and message.get("role") == "user"
    )
    assert effort_controls[0][0] < latest_user


@pytest.mark.live_openai_api(credential_env="ANTHROPIC_API_KEY")
async def test_live_anthropic_automatic_compaction_resumes_sampling(
    database_url: str, execution_target: str, codex_binary: str, code_mode_host: str, tmp_path: Path
) -> None:
    model = os.environ.get("ANTHROPIC_MODEL", DEFAULT_MODEL)
    base = settings(
        database_url, "https://unused.invalid", execution_target, codex_binary, code_mode_host, undeclared_outputs_dir()
    )
    config = anthropic_settings(
        base,
        model,
        max_tokens=1200,
        compaction=AnthropicCompactionConfig(max_tokens=1200, auto_compact_tokens=1, keep_recent_turns=0),
    ).model_copy(update={"turn_timeout_seconds": TURN_TIMEOUT_SECONDS})
    request = open_request(tmp_path)
    request.spec.model = ROUTE
    request.spec.reasoning_effort = "low"
    fact = f"the automatic-compaction code is {uuid4().hex}"
    initial_marker = f"ANTHROPIC_AUTO_COMPACT_FIRST_{uuid4().hex}"
    recall_marker = f"ANTHROPIC_AUTO_COMPACT_RECALL_{uuid4().hex}"
    stable_filler = "stable maple reference " * 2048

    async with service(config) as stub:
        await stub.OpenSession(request, metadata=METADATA, timeout=10)
        first = await submit(
            stub,
            request.session_id,
            "automatic-compact-first",
            f"{initial_marker}: remember this exact fact: {fact}. Reply SAVED. Reference text: {stable_filler}",
        )
        assert first.completed.status == event_pb2.TURN_STATUS_COMPLETED, first.completed.error
        second = await submit(
            stub,
            request.session_id,
            "automatic-compact-recall",
            f"{recall_marker}: what exact code did I ask you to remember? Reply with only the code.",
        )
        assert second.completed.status == event_pb2.TURN_STATUS_COMPLETED, second.completed.error
        state = await sampler_session(request.session_id, database_url)

    assert not state.blocked
    first_request = request_for_user_marker(state.intents, initial_marker)
    compaction_intents = [intent for intent in state.intents if intent.request.get("kind") == "compaction"]
    assert compaction_intents, "low Anthropic token threshold did not trigger automatic compaction"
    compaction_intent = compaction_intents[-1]
    compaction_body = body_of(compaction_intent)
    compaction_response = response_of(compaction_intent)
    assert compaction_body.get("compaction") == {"type": "summarize"}
    assert compaction_body.get("max_tokens") == 1200
    assert initial_marker in json.dumps(compaction_body.get("messages", []), sort_keys=True)
    assert recall_marker in json.dumps(compaction_body.get("messages", []), sort_keys=True)
    compaction_blocks = blocks_with_type(compaction_response, "compaction")
    assert len(compaction_blocks) == 1
    compaction_block = compaction_blocks[0]
    assert isinstance(compaction_block.get("signature"), str)
    assert compaction_block["signature"]
    assert compaction_response.get("stop_reason") == "compaction"
    assert compaction_response.get("content") == [compaction_block]

    sampling_after_compaction = []
    for intent in sampling_intents(state.intents):
        if intent.id == first_request.id:
            continue
        body = body_of(intent)
        messages = body.get("messages")
        if (
            isinstance(messages, list)
            and messages
            and messages[0] == {"role": "assistant", "content": [compaction_block]}
        ):
            sampling_after_compaction.append(intent)
    assert sampling_after_compaction, "automatic compaction did not resume provider sampling"
    followup_body = body_of(sampling_after_compaction[0])
    assert context_size(followup_body.get("messages")) < context_size(compaction_body.get("messages")), (
        "automatic compaction did not shrink the serialized active model context"
    )
    assert any(fact.rsplit(maxsplit=1)[-1] in text for text in second.assistant_text), second.assistant_text


async def sampler_session(session_id: str, database_url: str) -> SessionState:
    async with postgres_async_engine(database_url) as engine:
        store = Store(engine)
        return SessionState(blocked=(await store.get_session(session_id)).blocked, intents=await store.intents())


def context_size(value: Any) -> int:
    return len(json.dumps(value, separators=(",", ":")))


if __name__ == "__main__":
    pytest_bazel.main()
