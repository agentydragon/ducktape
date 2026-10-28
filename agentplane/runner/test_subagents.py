"""Native delegation stays within one Session without mixing child output into its transcript."""

from __future__ import annotations

import json

import pytest
import pytest_bazel

from agentplane.native.claude.testing import anthropic_sse
from agentplane.native.claude.testing.messages import AnthropicMessages
from agentplane.native.codex.testing import responses_sse
from agentplane.native.codex.testing.responses import OpenAIResponses
from agentplane.proto import event_pb2, runner_pb2
from agentplane.runner.testing import events
from agentplane.runner.testing.driver import RunnerDriver
from agentplane.runner.testing.fixtures import ModelEndpoint

# gazelle:include_dep @pypi//protobuf


@pytest.mark.parametrize("harness", [runner_pb2.HARNESS_CLAUDE], indirect=True)
async def test_claude_background_child_is_raw_only(
    driver: RunnerDriver, endpoint: ModelEndpoint, spec: runner_pb2.SessionSpec
) -> None:
    assert isinstance(endpoint, AnthropicMessages)
    session = await driver.attach("delegation", spec=spec)
    await session.submit_input("input-1", "Delegate a shell probe, then report its result.")
    async with await endpoint.await_next_request() as exchange:
        assert {"Agent", "SendMessage", "TaskOutput"} <= set(exchange.request.tool_names)
        stream = anthropic_sse.message_stream(
            [
                anthropic_sse.ToolUse(
                    "toolu_spawn",
                    "Agent",
                    {
                        "description": "Child probe",
                        "subagent_type": "general-purpose",
                        "prompt": "Run the shell probe and reply CHILD_DONE.",
                    },
                )
            ],
            model=spec.model,
        )
        await exchange.send(*stream.events)
    async with await endpoint.await_next_request() as first, await endpoint.await_next_request() as second:
        parent, child = (first, second) if first.request.tool_results else (second, first)
        assert parent.request.tool_results[0].tool_use_id == "toolu_spawn"
        assert not parent.request.tool_results[0].is_error
        await parent.send(
            *anthropic_sse.message_stream([anthropic_sse.Text("PARENT_WAITING")], model=spec.model).events
        )
        await parent.close()
        await session.until(events.turn_completed)
        await child.send(
            *anthropic_sse.message_stream(
                [anthropic_sse.ToolUse("toolu_child_shell", "Bash", {"command": "printf CHILD_TOOL_OUTPUT"})],
                model=spec.model,
            ).events
        )
    async with await endpoint.await_next_request() as exchange:
        assert "CHILD_TOOL_OUTPUT" in exchange.request.tool_results[0].text
        await session.until(
            lambda entry: (
                events.kind(entry) == "native"
                and "CHILD_TOOL_OUTPUT" in entry.event.native.line
                and "parent_tool_use_id" in entry.event.native.line
            )
        )
        assert not (await driver.attach("delegation")).attached.active_turn_id
        await exchange.send(*anthropic_sse.message_stream([anthropic_sse.Text("CHILD_DONE")], model=spec.model).events)
    async with await endpoint.await_next_request() as exchange:
        assert "CHILD_DONE" in "\n".join(exchange.request.texts("user"))
        await exchange.send(*anthropic_sse.message_stream([anthropic_sse.Text("PARENT_DONE")], model=spec.model).events)
    await session.until(events.turn_completed)
    assert len(events.of_kind(session.seen, "turn_started")) == 2
    assert len(events.of_kind(session.seen, "turn_completed")) == 2
    assert [
        events.completed(session.seen, item).text
        for item in events.items(session.seen, event_pb2.ITEM_KIND_ASSISTANT_TEXT)
    ] == ["PARENT_WAITING", "PARENT_DONE"]
    (delegation,) = events.items(session.seen, event_pb2.ITEM_KIND_TOOL_CALL)
    assert events.completed(session.seen, delegation).tool.succeeded
    assert len(await driver.list_sessions()) == 1
    events.assert_sourced(session.seen)


@pytest.mark.parametrize("harness", [runner_pb2.HARNESS_CODEX], indirect=True)
async def test_codex_child_completion_does_not_complete_parent(
    driver: RunnerDriver, endpoint: ModelEndpoint, spec: runner_pb2.SessionSpec
) -> None:
    assert isinstance(endpoint, OpenAIResponses)
    session = await driver.attach("delegation", spec=spec)
    await session.submit_input("input-1", "Delegate a shell probe and report its result.")
    async with await endpoint.await_next_request() as exchange:
        root_id = exchange.request.client_metadata.thread_id
        assert "multi_agent_v1" in exchange.request.tool_names
        await exchange.send(
            *responses_sse.response_stream(
                [
                    responses_sse.FunctionCall(
                        "call_spawn",
                        "spawn_agent",
                        {"message": "Run a shell probe and reply CHILD_DONE.", "fork_context": False},
                        namespace="multi_agent_v1",
                    )
                ],
                model=spec.model,
            ).events
        )
    async with await endpoint.await_next_request() as first, await endpoint.await_next_request() as second:
        parent, child = (first, second) if first.request.client_metadata.thread_id == root_id else (second, first)
        child_id = json.loads(parent.request.function_call_outputs[-1].text)["agent_id"]
        assert child.request.client_metadata.thread_id == child_id
        root_turn = (await driver.attach("delegation")).attached.active_turn_id
        assert root_turn
        await child.send(
            *responses_sse.response_stream(
                [responses_sse.FunctionCall("call_child_shell", "exec_command", {"cmd": "printf CHILD_TOOL_OUTPUT"})],
                model=spec.model,
            ).events
        )
        await child.close()
        async with await endpoint.await_next_request() as exchange:
            assert exchange.request.client_metadata.thread_id == child_id
            assert "CHILD_TOOL_OUTPUT" in exchange.request.function_call_outputs[-1].output
            await exchange.send(
                *responses_sse.response_stream([responses_sse.Message("CHILD_DONE")], model=spec.model).events
            )
        await session.until(
            lambda entry: (
                events.kind(entry) == "native"
                and entry.event.native.direction == event_pb2.DIRECTION_FROM_HARNESS
                and (frame := json.loads(entry.event.native.line)).get("method") == "turn/completed"
                and frame["params"]["threadId"] == child_id
            )
        )
        assert (await driver.attach("delegation")).attached.active_turn_id == root_turn
        assert not events.of_kind(session.seen, "turn_completed")
        assert not events.items(session.seen, event_pb2.ITEM_KIND_ASSISTANT_TEXT)
        assert not events.items(session.seen, event_pb2.ITEM_KIND_REASONING)
        (delegation,) = events.items(session.seen, event_pb2.ITEM_KIND_TOOL_CALL)
        assert events.completed(session.seen, delegation).tool.succeeded
        assert len(await driver.list_sessions()) == 1
        await session.interrupt_turn("interrupt-parent", root_turn)
        done = await session.until(events.turn_completed)
        assert done.event.turn_completed.turn_id == root_turn
        assert done.event.turn_completed.status == event_pb2.TURN_STATUS_INTERRUPTED
    events.assert_sourced(session.seen)


if __name__ == "__main__":
    pytest_bazel.main()
