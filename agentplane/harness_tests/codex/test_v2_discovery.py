"""Read-only discovery of multi_agent_v2 children, separate from model-driven reactivation."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Any
from uuid import uuid4

import pytest
import pytest_bazel
from pydantic import BaseModel

from agentplane.harness_tests.codex import responses_sse as sse
from agentplane.harness_tests.codex.harness import EFFORT, MODEL, CodexHarness
from agentplane.harness_tests.codex.responses import OpenAIResponses
from agentplane.native.async_process import AsyncNativeProcess
from agentplane.native.codex import driver, scenarios


class ProbeRequest(BaseModel):
    """Experimental RPCs retain their full response in the native trace."""

    id: str
    method: str
    params: dict[str, Any]


async def rpc(process: AsyncNativeProcess, method: str, **params: Any) -> dict[str, Any]:
    request = ProbeRequest(id=str(uuid4()), method=method, params=params)
    receipt = await process.request(request, matches=lambda frame: frame.get("id") == request.id)
    assert "error" not in receipt.frame, receipt.frame
    result = receipt.frame["result"]
    assert isinstance(result, dict)
    return result


@asynccontextmanager
async def server(codex: CodexHarness, model: OpenAIResponses, incarnation: str) -> AsyncIterator[AsyncNativeProcess]:
    endpoint = f"{model.origin}/v1"
    logs = codex.logs / incarnation
    logs.mkdir()
    async with AsyncNativeProcess(
        logs,
        scenarios.command(codex.binary, endpoint=endpoint, max_retries=scenarios.MAX_RETRIES),
        cwd=codex.workspace,
        environment={
            **codex.base_environment,
            **scenarios.environment(endpoint=endpoint, token="test-key", codex_home=str(codex.codex_home)),
        },
    ) as process:
        await rpc(
            process,
            "initialize",
            clientInfo={"name": "agentplane-v2-probe", "version": "0.1"},
            capabilities={"experimentalApi": True},
        )
        await process.send(driver.initialized())
        yield process


async def listed(process: AsyncNativeProcess, method: str, **params: Any) -> list[Any]:
    """Consume every page rather than mistaking the first page for a complete snapshot."""
    result: list[Any] = []
    cursor = None
    while True:
        page = await rpc(process, method, cursor=cursor, limit=1, **params)
        result.extend(page["data"])
        cursor = page.get("nextCursor")
        if cursor is None:
            return result


@pytest.mark.parametrize("scenario", ["clean-completed", "crash-completed", "crash-active"])
async def test_v2_child_discovery_and_resume(
    codex: CodexHarness, openai_responses: OpenAIResponses, scenario: str
) -> None:
    active = scenario == "crash-active"
    async with asyncio.timeout(90):
        async with server(codex, openai_responses, "initial") as process:
            root = (
                await rpc(
                    process,
                    "thread/start",
                    cwd=str(codex.workspace),
                    model=MODEL,
                    approvalPolicy="never",
                    sandbox="danger-full-access",
                    ephemeral=False,
                    baseInstructions=scenarios.BASE_INSTRUCTIONS,
                    config={
                        "model_reasoning_effort": EFFORT,
                        "features.multi_agent": True,
                        "features.multi_agent_v2": True,
                    },
                )
            )["thread"]
            root_id = root["id"]
            assert root_id
            assert root["parentThreadId"] is None
            assert root_id in await listed(process, "thread/loaded/list")
            await rpc(process, "turn/start", threadId=root_id, input=[{"type": "text", "text": "Delegate a probe."}])
            async with await openai_responses.await_next_request() as exchange:
                assert "collaboration" in exchange.request.tool_names
                await exchange.send(
                    *sse.response_stream(
                        [
                            sse.FunctionCall(
                                "call_v2_spawn",
                                "spawn_agent",
                                {"message": "Reply V2_CHILD_DONE.", "task_name": "probe", "fork_turns": "none"},
                                namespace="collaboration",
                            )
                        ],
                        model=MODEL,
                    ).events
                )
            async with AsyncExitStack() as exchanges:
                # Keep both requests open until their native identities are known. Arrival
                # order is not a parent/child discriminator.
                first = await exchanges.enter_async_context(await openai_responses.await_next_request())
                second = await exchanges.enter_async_context(await openai_responses.await_next_request())
                parent, child = (
                    (first, second) if first.request.client_metadata.thread_id == root_id else (second, first)
                )
                assert parent.request.client_metadata.thread_id == root_id
                child_id = child.request.client_metadata.thread_id
                assert child_id != root_id
                assert parent.request.function_call_outputs[-1].call_id == "call_v2_spawn"
                assert json.loads(parent.request.function_call_outputs[-1].output)["task_name"]
                before = (await rpc(process, "thread/read", threadId=child_id, includeTurns=False))["thread"]
                assert before["parentThreadId"] == root_id
                assert before["sessionId"] == root["sessionId"]
                assert before["status"]["type"] == "active"
                assert {root_id, child_id} <= set(await listed(process, "thread/loaded/list"))
                await parent.send(*sse.response_stream([sse.Message("V2_PARENT_DONE")], model=MODEL).events)
                if active:
                    assert await process.crash() < 0
                    await child.wait_client_closed()
                else:
                    await child.send(*sse.response_stream([sse.Message("V2_CHILD_DONE")], model=MODEL).events)
                    # A model response finishing is not yet a native turn completion.
                    while True:
                        finished = (await rpc(process, "thread/read", threadId=child_id, includeTurns=True))["thread"]
                        if finished["status"]["type"] == "idle":
                            break
                        await asyncio.sleep(0.05)
                    assert finished["turns"][-1]["status"] == "completed"
                    assert "V2_CHILD_DONE" in json.dumps(finished["turns"])
                    if scenario == "crash-completed":
                        assert await process.crash() < 0
            spawn_items = [
                frame["params"]
                for frame in process.stdout_frames()
                if frame.get("method") == "item/completed" and frame["params"]["item"]["id"] == "call_v2_spawn"
            ]
            (spawn,) = spawn_items
            assert spawn["threadId"] == root_id
            assert spawn["item"] == {
                "type": "subAgentActivity",
                "id": "call_v2_spawn",
                "kind": "started",
                "agentThreadId": child_id,
                "agentPath": "/root/probe",
            }
        async with server(codex, openai_responses, "resumed") as resumed:
            assert await listed(resumed, "thread/loaded/list") == []
            restored = (await rpc(resumed, "thread/resume", threadId=root_id, excludeTurns=True))["thread"]
            assert restored["id"] == root_id
            loaded_before = await listed(resumed, "thread/loaded/list")
            # No turn/start, followup_task, child resume, or model-driven status query:
            # measure persisted discovery separately from lazy child reactivation.
            children = await listed(resumed, "thread/list", sourceKinds=["subAgent"], modelProviders=[])
            assert children == []
            descendants = await listed(resumed, "thread/list", parentThreadId=root_id, modelProviders=[])
            recovered = (await rpc(resumed, "thread/read", threadId=child_id, includeTurns=True))["thread"]
            (codex.logs / "recovery.json").write_text(json.dumps(recovered, indent=2) + "\n")
            assert child_id in {thread["id"] for thread in descendants}
            assert recovered["parentThreadId"] == root_id
            # Loaded children share the root session ID, but an unloaded read
            # reports the child ID. Parent linkage is the durable tree evidence.
            assert recovered["sessionId"] == child_id
            assert recovered["status"]["type"] == "notLoaded"
            assert child_id not in loaded_before
            assert await listed(resumed, "thread/loaded/list") == loaded_before
            if not active:
                assert recovered["turns"][-1]["status"] == "completed"
                assert "V2_CHILD_DONE" in json.dumps(recovered["turns"])
            else:
                assert recovered["turns"][-1]["status"] == "interrupted"
                assert recovered["turns"][-1]["completedAt"] is None
                assert "V2_CHILD_DONE" not in json.dumps(recovered["turns"])
            # notLoaded alone is not evidence of completion or cancellation.
            with pytest.raises(TimeoutError):
                async with asyncio.timeout(0.25):
                    await openai_responses.await_next_request()


if __name__ == "__main__":
    pytest_bazel.main()
