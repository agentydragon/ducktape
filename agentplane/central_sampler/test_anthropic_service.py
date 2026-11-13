"""Postgres-backed failure atomicity for the native Anthropic Messages adapter."""

import asyncio
import json
from pathlib import Path
from typing import Any

import grpc
import pytest
import pytest_bazel
from pydantic import SecretStr
from sqlalchemy import text

from agentplane.central_sampler.api import sampler_pb2
from agentplane.central_sampler.settings import (
    AnthropicCompactionConfig,
    AnthropicMessagesConfig,
    AnthropicThinkingConfig,
    AuthConfig,
    ConnectionConfig,
    ModelConfig,
)
from agentplane.central_sampler.testing import (
    METADATA,
    SESSION_ID,
    Endpoint,
    create_request,
    service,
    settings,
    turn_completed,
)
from agentplane.central_sampler.testing_store import Store
from agentplane.env.runtime.control import environment_runtime_pb2
from agentplane.native.testing.model_endpoint import SseEvent
from agentplane.proto import event_pb2
from devinfra.testing.undeclared_outputs import undeclared_outputs_dir
from util.testing.sqlalchemy_engines import postgres_async_engine

ROUTE = "anthropic-native-test"
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


async def wait_command_outcome(stub: Any, command_id: str, expected: int) -> Any:
    deadline = asyncio.get_running_loop().time() + 10
    while True:
        result = await stub.GetCommand(
            sampler_pb2.GetCommandRequest(session_id=SESSION_ID, command_id=command_id), metadata=METADATA, timeout=10
        )
        if result.outcome == expected:
            return result
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError(f"command {command_id!r} did not reach outcome {expected}: {result}")
        await asyncio.sleep(0.05)


async def provider_attempts(engine: Any, command_id: str) -> list[dict[str, Any]]:
    async with engine.connect() as connection:
        rows = (
            (
                await connection.execute(
                    text(
                        "SELECT id::text AS id, provider_step_id::text AS step_id, provider_attempt_no AS number, "
                        "provider_attempt_budget AS budget, previous_provider_intent_id::text AS previous_id, "
                        "provider_retry_not_before IS NOT NULL AS retry_scheduled, result::text AS result "
                        "FROM sampler_intent WHERE provider_command_id=:command_id ORDER BY provider_attempt_no"
                    ),
                    {"command_id": command_id},
                )
            )
            .mappings()
            .all()
        )
    return [
        {
            "id": row.id,
            "step_id": row.step_id,
            "number": row.number,
            "budget": row.budget,
            "previous_id": row.previous_id,
            "retry_scheduled": row.retry_scheduled,
            "result": json.loads(row.result),
        }
        for row in rows
    ]


async def assert_reconstructed_request(stub: Any, request_id: str, expected: dict[str, Any]) -> None:
    request = sampler_pb2.ReadProviderRequestRequest(session_id=SESSION_ID, request_id=request_id)
    chunks = [chunk async for chunk in stub.ReadProviderRequest(request, metadata=METADATA, timeout=10)]
    assert chunks[0].metadata.request_id == request_id
    assert "x-api-key" not in chunks[0].metadata.headers
    assert json.loads(b"".join(chunk.body_chunk for chunk in chunks[1:])) == expected
    with pytest.raises(grpc.aio.AioRpcError) as denied:
        _ = [
            chunk
            async for chunk in stub.ReadProviderRequest(
                request, metadata=(("authorization", "Bearer recreated-account-token"),), timeout=10
            )
        ]
    assert denied.value.code() == grpc.StatusCode.NOT_FOUND


@pytest.mark.parametrize("operation", ["partial_exec", "failed_compaction"])
async def test_anthropic_truncated_stream_retries_without_committing_partial_data(
    operation: str, database_url: str, execution_target: str, codex_binary: str, code_mode_host: str, tmp_path: Path
) -> None:
    async with postgres_async_engine(database_url) as engine, Endpoint() as endpoint:
        store = Store(engine)
        base = settings(
            database_url, endpoint.origin, execution_target, codex_binary, code_mode_host, undeclared_outputs_dir()
        )
        config = native_config(base)
        async with service(config) as stub:
            request = create_request(tmp_path)
            request.session_id = SESSION_ID
            request.spec.model = ROUTE
            request.spec.reasoning_effort = "low"
            await stub.CreateSession(request, metadata=METADATA, timeout=10)

            if operation == "failed_compaction":
                await stub.SubmitInput(
                    environment_runtime_pb2.SubmitInputRequest(
                        session_id=SESSION_ID, command_id="anthropic-seed", text="Save the pre-compaction context."
                    ),
                    metadata=METADATA,
                    timeout=10,
                )
                async with await endpoint.next() as exchange:
                    assert exchange.request.body["stream"] is True
                    assert exchange.headers.get("x-api-key") == "fixture-anthropic-key"
                    await exchange.send(*end_turn_events("msg_seed"))
                assert (
                    await turn_completed(stub, SESSION_ID, "anthropic-seed", timeout_s=TIMEOUT_SECONDS)
                ).status == event_pb2.TURN_STATUS_COMPLETED
                transcript_before = await store.transcript_items(SESSION_ID)
                active_before = await store.active_context(SESSION_ID)
                await stub.Compact(
                    sampler_pb2.CompactRequest(session_id=SESSION_ID, command_id="anthropic-compact-failed"),
                    metadata=METADATA,
                    timeout=10,
                )
                compact_request = None
                for attempt in range(3):
                    async with asyncio.timeout(15):
                        exchange = await endpoint.next()
                    async with exchange:
                        current_request = exchange.request.body
                        assert current_request["compaction"] == {"type": "summarize"}
                        compact_messages = current_request.get("messages")
                        assert isinstance(compact_messages, list)
                        assert compact_messages
                        if compact_request is None:
                            compact_request = current_request
                        else:
                            assert current_request == compact_request, "compaction retry changed the frozen request"
                        await exchange.send(
                            message_start(f"msg_compaction_incomplete_{attempt + 1}"),
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
                assert compact_request is not None
                command = await wait_command_outcome(
                    stub, "anthropic-compact-failed", sampler_pb2.COMMAND_OUTCOME_FAILED
                )
                assert command.failure_reason
                assert await store.transcript_items(SESSION_ID) == transcript_before, (
                    "failed compaction changed the transcript"
                )
                assert await store.active_context(SESSION_ID) == active_before, (
                    "failed compaction changed active context"
                )
                attempts = await provider_attempts(engine, "anthropic-compact-failed")
                assert len(attempts) == 3
                assert [attempt["number"] for attempt in attempts] == [1, 2, 3]
                assert {attempt["step_id"] for attempt in attempts} == {attempts[0]["step_id"]}
                assert [attempt["previous_id"] for attempt in attempts] == [None, attempts[0]["id"], attempts[1]["id"]]
                assert [attempt["budget"] for attempt in attempts] == [3, 3, 3]
                assert [attempt["retry_scheduled"] for attempt in attempts] == [True, True, False]
                assert all(attempt["result"]["status"] == "abandoned" for attempt in attempts)
                intents = await store.intents()
                compaction_intents = [intent for intent in intents if intent.request.get("kind") == "compaction"]
                assert len(compaction_intents) == 3
                for intent in compaction_intents:
                    assert intent.body == compact_request, (
                        "reconstructed content differs from the captured provider request"
                    )
                    assert intent.request.get("profile_id"), "compact request has no stored profile reference"
                    assert "body" not in intent.request
                    assert "wire_request" not in intent.request
                    await assert_reconstructed_request(stub, intent.id, compact_request)
                snapshot = await stub.GetSession(
                    sampler_pb2.GetSessionRequest(session_id=SESSION_ID), metadata=METADATA
                )
                assert not snapshot.blocked, "exhausting provider retries blocked the Session"

                await stub.SubmitInput(
                    environment_runtime_pb2.SubmitInputRequest(
                        session_id=SESSION_ID,
                        command_id="input-after-failed-compaction",
                        text="Continue after compaction failure.",
                    ),
                    metadata=METADATA,
                    timeout=10,
                )
                async with await endpoint.next() as exchange:
                    next_body = exchange.request.body
                    assert "Continue after compaction failure." in json.dumps(next_body)
                    await exchange.send(*end_turn_events("msg_after_compaction_failure"))
                assert (
                    await turn_completed(stub, SESSION_ID, "input-after-failed-compaction", timeout_s=TIMEOUT_SECONDS)
                ).status == event_pb2.TURN_STATUS_COMPLETED
                await wait_command_outcome(stub, "input-after-failed-compaction", sampler_pb2.COMMAND_OUTCOME_APPLIED)
            else:
                await stub.SubmitInput(
                    environment_runtime_pb2.SubmitInputRequest(
                        session_id=SESSION_ID, command_id=TURN_ID, text="Ask the eager JSON tool to run once."
                    ),
                    metadata=METADATA,
                    timeout=10,
                )
                body = None
                for attempt in range(3):
                    async with asyncio.timeout(15):
                        exchange = await endpoint.next()
                    async with exchange:
                        current_body = exchange.request.body
                        assert exchange.headers.get("x-api-key") == "fixture-anthropic-key"
                        assert exchange.headers.get("anthropic-version") == "2023-06-01"
                        assert current_body["tools"][0]["eager_input_streaming"] is True
                        if body is None:
                            body = current_body
                        else:
                            assert current_body == body, "sampling retry changed the frozen request"
                        await exchange.send(
                            message_start(f"msg_tool_incomplete_{attempt + 1}"),
                            anthropic_event(
                                "content_block_start",
                                index=0,
                                content_block={
                                    "type": "tool_use",
                                    "id": "call_uncommitted",
                                    "name": "exec",
                                    "input": {},
                                },
                            ),
                            anthropic_event(
                                "content_block_delta",
                                index=0,
                                delta={
                                    "type": "input_json_delta",
                                    "partial_json": '{"code":"text(\'must not execute\')"}',
                                },
                            ),
                            anthropic_event("content_block_stop", index=0),
                            anthropic_event(
                                "message_delta", delta={"stop_reason": "tool_use"}, usage={"output_tokens": 4}
                            ),
                        )
                        await exchange.close()
                assert body is not None
                terminal = await turn_completed(stub, SESSION_ID, TURN_ID, timeout_s=TIMEOUT_SECONDS)
                assert terminal.status == event_pb2.TURN_STATUS_FAILED
                command = await wait_command_outcome(stub, TURN_ID, sampler_pb2.COMMAND_OUTCOME_FAILED)
                assert command.failure_reason
                assert not (await store.get_session(SESSION_ID)).blocked
                attempts = await provider_attempts(engine, TURN_ID)
                assert len(attempts) == 3
                assert [attempt["number"] for attempt in attempts] == [1, 2, 3]
                assert {attempt["step_id"] for attempt in attempts} == {attempts[0]["step_id"]}
                assert [attempt["previous_id"] for attempt in attempts] == [None, attempts[0]["id"], attempts[1]["id"]]
                assert [attempt["budget"] for attempt in attempts] == [3, 3, 3]
                assert [attempt["retry_scheduled"] for attempt in attempts] == [True, True, False]
                assert all(attempt["result"]["status"] == "abandoned" for attempt in attempts)
                intents = await store.intents()
                sampling_intents = [intent for intent in intents if intent.request.get("kind") == "sampling"]
                assert len(sampling_intents) == 3
                for intent in sampling_intents:
                    assert intent.body == body, "reconstructed content differs from the captured provider request"
                    assert intent.request.get("profile_id"), "sampling request has no stored profile reference"
                    assert "body" not in intent.request
                    assert "wire_request" not in intent.request
                    await assert_reconstructed_request(stub, intent.id, body)
                assert not any(intent.request.get("kind") == "tool" for intent in intents), (
                    "partial tool arguments crossed the terminal provider-message boundary"
                )
                assert not any(item.get("role") == "assistant" for item in await store.transcript_items(SESSION_ID)), (
                    "an incomplete provider response was appended to canonical context"
                )
                assert not await store.has_live_cell(SESSION_ID), "partial tool call dispatched an executor cell"

                await stub.SubmitInput(
                    environment_runtime_pb2.SubmitInputRequest(
                        session_id=SESSION_ID,
                        command_id="input-after-truncated-sampling",
                        text="Continue after provider failure.",
                    ),
                    metadata=METADATA,
                    timeout=10,
                )
                async with await endpoint.next() as exchange:
                    assert "Continue after provider failure." in json.dumps(exchange.request.body)
                    await exchange.send(*end_turn_events("msg_after_sampling_failure"))
                assert (
                    await turn_completed(stub, SESSION_ID, "input-after-truncated-sampling", timeout_s=TIMEOUT_SECONDS)
                ).status == event_pb2.TURN_STATUS_COMPLETED
                await wait_command_outcome(stub, "input-after-truncated-sampling", sampler_pb2.COMMAND_OUTCOME_APPLIED)


# gazelle:include_dep @pypi//grpcio
# gazelle:include_dep @pypi//protobuf

if __name__ == "__main__":
    pytest_bazel.main()
