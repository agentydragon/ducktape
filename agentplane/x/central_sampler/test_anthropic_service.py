"""Postgres-backed failure atomicity for the native Anthropic Messages adapter."""

import asyncio
import json
from pathlib import Path
from typing import Any, cast

import pytest
import pytest_bazel
from pydantic import SecretStr

from agentplane.native.testing.model_endpoint import SseEvent
from agentplane.proto import event_pb2
from agentplane.runner import runner_service_pb2
from agentplane.x.central_sampler import sampler_pb2
from agentplane.x.central_sampler.settings import (
    AnthropicCompactionConfig,
    AnthropicMessagesConfig,
    AnthropicThinkingConfig,
    AuthConfig,
    ConnectionConfig,
    ModelConfig,
)
from agentplane.x.central_sampler.testing import METADATA, Endpoint, open_request, service, settings
from agentplane.x.central_sampler.testing_store import Store
from util.testing.sqlalchemy_engines import postgres_async_engine
from util.testing.undeclared_outputs import undeclared_outputs_dir

ROUTE = "anthropic-native-test"
SESSION_ID = "079bd706-77b3-4b71-8c7e-605a7e19324c"
TURN_ID = "anthropic-truncated"
TIMEOUT_SECONDS = 40


def anthropic_event(kind: str, **fields: Any) -> SseEvent:
    payload = {"type": kind, **fields}
    return SseEvent(kind, f"data: {json.dumps(payload, separators=(',', ':'))}\n\n".encode())


def native_config(base: Any) -> Any:
    base.connections["anthropic"] = ConnectionConfig(
        base_url=base.connections["provider"].base_url,
        auth=AuthConfig(type="anthropic_api_key", credential=SecretStr("fixture-anthropic-key")),
    )
    base.models[ROUTE] = ModelConfig(
        connection="anthropic",
        provider_model="claude-fixture",
        reasoning_efforts={"low", "high"},
        api="anthropic_messages",
        exec_encoding="json",
        anthropic_messages=AnthropicMessagesConfig(
            max_tokens=256,
            thinking=AnthropicThinkingConfig(type="adaptive"),
            strict_tool_input=True,
            eager_input_streaming=True,
            compaction=AnthropicCompactionConfig(max_tokens=128),
        ),
    )
    base.turn_timeout_seconds = TIMEOUT_SECONDS
    return base


def message_start(message_id: str) -> SseEvent:
    return anthropic_event(
        "message_start",
        message={
            "id": message_id,
            "type": "message",
            "role": "assistant",
            "content": [],
            "model": "claude-fixture",
            "stop_reason": None,
            "stop_sequence": None,
            "usage": {"input_tokens": 7, "output_tokens": 0},
        },
    )


def end_turn_events(message_id: str) -> tuple[SseEvent, ...]:
    return (
        message_start(message_id),
        anthropic_event("content_block_start", index=0, content_block={"type": "text", "text": ""}),
        anthropic_event("content_block_delta", index=0, delta={"type": "text_delta", "text": "seed context"}),
        anthropic_event("content_block_stop", index=0),
        anthropic_event(
            "message_delta", delta={"stop_reason": "end_turn", "stop_sequence": None}, usage={"output_tokens": 3}
        ),
        anthropic_event("message_stop"),
    )


async def completed(stub: Any, turn_id: str) -> event_pb2.TurnCompleted:
    stream = stub.FollowEvents(sampler_pb2.FollowEventsRequest(session_id=SESSION_ID, follow=True), metadata=METADATA)
    try:
        async with asyncio.timeout(TIMEOUT_SECONDS):
            async for frame in stream:
                event = frame.entry.event
                if event.HasField("turn_completed") and event.turn_completed.turn_id == turn_id:
                    return cast(event_pb2.TurnCompleted, event.turn_completed)
    finally:
        stream.cancel()
    raise AssertionError("event stream ended before the turn completed")


async def wait_until_blocked(stub: Any) -> None:
    async with asyncio.timeout(10):
        while True:
            snapshot = await stub.GetSession(sampler_pb2.GetSessionRequest(session_id=SESSION_ID), metadata=METADATA)
            if snapshot.blocked:
                return
            await asyncio.sleep(0.05)


@pytest.mark.parametrize("operation", ["partial_exec", "failed_compaction"])
async def test_anthropic_missing_terminal_is_unknown_and_does_not_commit(
    operation: str, database_url: str, execution_target: str, codex_binary: str, code_mode_host: str, tmp_path: Path
) -> None:
    async with postgres_async_engine(database_url) as engine, Endpoint() as endpoint:
        store = Store(engine)
        base = settings(
            database_url, endpoint.origin, execution_target, codex_binary, code_mode_host, undeclared_outputs_dir()
        )
        config = native_config(base)
        async with service(config) as stub:
            request = open_request(tmp_path)
            request.session_id = SESSION_ID
            request.spec.model = ROUTE
            request.spec.reasoning_effort = "low"
            await stub.OpenSession(request, metadata=METADATA, timeout=10)

            if operation == "failed_compaction":
                await stub.SubmitInput(
                    runner_service_pb2.SubmitInputRequest(
                        session_id=SESSION_ID, command_id="anthropic-seed", text="Save the pre-compaction context."
                    ),
                    metadata=METADATA,
                    timeout=10,
                )
                async with await endpoint.next() as exchange:
                    assert exchange.request.body["stream"] is True
                    assert exchange.headers.get("x-api-key") == "fixture-anthropic-key"
                    await exchange.send(*end_turn_events("msg_seed"))
                assert (await completed(stub, "anthropic-seed")).status == event_pb2.TURN_STATUS_COMPLETED
                archive_before = await store.archive_items(SESSION_ID)
                active_before = await store.active_context(SESSION_ID)
                await stub.Compact(
                    sampler_pb2.CompactRequest(session_id=SESSION_ID, command_id="anthropic-compact-failed"),
                    metadata=METADATA,
                    timeout=10,
                )
                async with await endpoint.next() as exchange:
                    compact_request = exchange.request.body
                    assert compact_request["compaction"] == {"type": "summarize"}
                    compact_messages = compact_request.get("messages")
                    assert isinstance(compact_messages, list)
                    assert compact_messages
                    await exchange.send(
                        message_start("msg_compaction_incomplete"),
                        anthropic_event(
                            "content_block_start",
                            index=0,
                            content_block={
                                "type": "compaction",
                                "content": "summary that must not be committed",
                                "signature": "fixture-signature",
                            },
                        ),
                        anthropic_event("content_block_stop", index=0),
                        anthropic_event(
                            "message_delta", delta={"stop_reason": "compaction"}, usage={"output_tokens": 5}
                        ),
                    )
                    await exchange.close()
                await wait_until_blocked(stub)
                assert await store.archive_items(SESSION_ID) == archive_before, "failed compaction changed the archive"
                assert await store.active_context(SESSION_ID) == active_before, (
                    "failed compaction changed active context"
                )
                intents = await store.intents()
                compaction_intents = [intent for intent in intents if intent.request.get("kind") == "compaction"]
                assert len(compaction_intents) == 1
                intent = compaction_intents[0]
                assert intent.result is None, "a non-terminal compaction was recorded as a settled result"
                assert json.loads(intent.request["wire_request"]) == intent.request["body"]
                assert (await store.get_session(SESSION_ID)).blocked
            else:
                await stub.SubmitInput(
                    runner_service_pb2.SubmitInputRequest(
                        session_id=SESSION_ID, command_id=TURN_ID, text="Ask the eager JSON tool to run once."
                    ),
                    metadata=METADATA,
                    timeout=10,
                )
                async with await endpoint.next() as exchange:
                    body = exchange.request.body
                    assert exchange.headers.get("x-api-key") == "fixture-anthropic-key"
                    assert exchange.headers.get("anthropic-version") == "2023-06-01"
                    assert body["tools"][0]["eager_input_streaming"] is True
                    await exchange.send(
                        message_start("msg_tool_incomplete"),
                        anthropic_event(
                            "content_block_start",
                            index=0,
                            content_block={"type": "tool_use", "id": "call_uncommitted", "name": "exec", "input": {}},
                        ),
                        anthropic_event(
                            "content_block_delta",
                            index=0,
                            delta={"type": "input_json_delta", "partial_json": '{"code":"text(\'must not execute\')"}'},
                        ),
                        anthropic_event("content_block_stop", index=0),
                        anthropic_event("message_delta", delta={"stop_reason": "tool_use"}, usage={"output_tokens": 4}),
                    )
                    await exchange.close()
                terminal = await completed(stub, TURN_ID)
                assert terminal.status == event_pb2.TURN_STATUS_FAILED
                assert (await store.get_session(SESSION_ID)).blocked
                intents = await store.intents()
                assert len(intents) == 1
                assert intents[0].request.get("kind") == "sampling"
                assert intents[0].result is None, "a non-terminal eager tool call was recorded as settled"
                assert json.loads(intents[0].request["wire_request"]) == intents[0].request["body"]
                assert not any(intent.request.get("kind") == "tool" for intent in intents), (
                    "partial tool arguments crossed the terminal provider-message boundary"
                )
                assert not any(
                    item.get("type") == "anthropic_message" for item in await store.archive_items(SESSION_ID)
                ), "an incomplete provider response was appended to canonical context"


if __name__ == "__main__":
    pytest_bazel.main()
