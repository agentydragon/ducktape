"""Native child discovery, streaming, ancestry and persisted recovery for v1 and v2.

After completed and active-child restart scenarios, `parentThreadId` finds persisted children even
though the `subAgent` source filter and loaded-thread list are empty. `thread/read` distinguishes a
completed child from an interrupted one; `notLoaded` alone does not identify its fate, and the
unloaded `sessionId` is not a durable parent/child key. The bounded read-only phase starts no model
request.

Both versions automatically stream newly created children to the initialized connection. Explicit
resume of a loaded child does not duplicate completed items. V1 permits a grandchild with max_depth=2;
the tested v2 fresh-child configuration omits collaboration tools. Neither absence of a loaded
thread nor parent failure proves a child's terminal outcome.
"""

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

from agentplane.native.codex import driver, scenarios, wire
from agentplane.native.codex.testing import responses_sse
from agentplane.native.codex.testing.harness import CodexHarness
from agentplane.native.codex.testing.model import EFFORT, MODEL
from agentplane.native.codex.testing.responses import OpenAIResponses
from agentplane.native.testing.async_process import AsyncNativeProcess


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
                    *responses_sse.response_stream(
                        [
                            responses_sse.FunctionCall(
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
                assert json.loads(parent.request.function_call_outputs[-1].text)["task_name"]
                before = (await rpc(process, "thread/read", threadId=child_id, includeTurns=False))["thread"]
                assert before["parentThreadId"] == root_id
                assert before["sessionId"] == root["sessionId"]
                assert before["status"]["type"] == "active"
                assert {root_id, child_id} <= set(await listed(process, "thread/loaded/list"))
                await parent.send(
                    *responses_sse.response_stream([responses_sse.Message("V2_PARENT_DONE")], model=MODEL).events
                )
                if active:
                    assert await process.crash() < 0
                    await child.wait_client_closed()
                else:
                    await child.send(
                        *responses_sse.response_stream([responses_sse.Message("V2_CHILD_DONE")], model=MODEL).events
                    )
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
                "model": MODEL,
                "reasoningEffort": EFFORT,
            }
            spawn_model = wire.SubAgentActivityItem.model_validate(spawn["item"])
            assert spawn_model.model == MODEL
            assert spawn_model.reasoning_effort == EFFORT
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


@pytest.mark.parametrize("v2", [False, True], ids=["v1", "v2"])
@pytest.mark.parametrize("attach", [False, True], ids=["parent-only", "child-resume"])
async def test_concurrent_child_transcripts_and_recovery(
    codex: CodexHarness, openai_responses: OpenAIResponses, v2: bool, attach: bool
) -> None:
    """Hold siblings at model boundaries; finish in reverse order, then read after a crash."""
    namespace = "collaboration" if v2 else "multi_agent_v1"
    child_ids: dict[str, str] = {}
    completed_items: dict[str, list[dict[str, Any]]] = {}
    async with asyncio.timeout(90):
        async with server(codex, openai_responses, "siblings") as process:
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
                        "features.multi_agent_v2": v2,
                    },
                )
            )["thread"]
            root_id = root["id"]
            await rpc(process, "turn/start", threadId=root_id, input=[{"type": "text", "text": "Delegate two probes."}])
            async with AsyncExitStack() as exchanges:
                parent = await exchanges.enter_async_context(await openai_responses.await_next_request())
                children = {}
                for name in ("first", "second"):
                    assert namespace in parent.request.tool_names
                    arguments: dict[str, Any] = {"message": f"CHILD_{name}_PROMPT"}
                    if v2:
                        arguments.update(task_name=name, fork_turns="none")
                    else:
                        arguments["fork_context"] = False
                    await parent.send(
                        *responses_sse.response_stream(
                            [
                                responses_sse.FunctionCall(
                                    f"spawn_{name}", "spawn_agent", arguments, namespace=namespace
                                )
                            ],
                            model=MODEL,
                        ).events
                    )
                    await parent.close()
                    first = await exchanges.enter_async_context(await openai_responses.await_next_request())
                    second = await exchanges.enter_async_context(await openai_responses.await_next_request())
                    parent, child = (
                        (first, second) if first.request.client_metadata.thread_id == root_id else (second, first)
                    )
                    assert parent.request.client_metadata.thread_id == root_id
                    child_id = child.request.client_metadata.thread_id
                    assert child_id != root_id
                    assert f"CHILD_{name}_PROMPT" in child.request.model_dump_json()
                    child_ids[name] = child_id
                    children[name] = child
                    assert parent.request.function_call_outputs[-1].call_id == f"spawn_{name}"
                    if not v2:
                        assert json.loads(parent.request.function_call_outputs[-1].text)["agent_id"] == child_id
                    snapshot = (await rpc(process, "thread/read", threadId=child_id, includeTurns=False))["thread"]
                    assert snapshot["status"]["type"] == "active"
                    assert snapshot["parentThreadId"] == root_id
                    if attach:
                        attached = (await rpc(process, "thread/resume", threadId=child_id))["thread"]
                        assert attached["id"] == child_id
                assert len(set(child_ids.values())) == 2
                for name in ("second", "first"):
                    child_id = child_ids[name]
                    child = children[name]
                    observer = process.frames()
                    stream = responses_sse.response_stream(
                        [
                            responses_sse.Reasoning(f"THINK_{name}", f"opaque_{name}"),
                            responses_sse.Message(f"LIVE_{name}"),
                            responses_sse.FunctionCall(
                                f"shell_{name}",
                                "exec_command",
                                {"cmd": f"printf OUT_{name}; printf ERR_{name} >&2; exit 23"},
                            ),
                        ],
                        model=MODEL,
                    )
                    prefix = stream.through("response.output_text.delta")
                    await child.send(*prefix.events)
                    while True:
                        frame = await observer.next()
                        if (
                            frame.get("method") == "item/agentMessage/delta"
                            and frame["params"]["delta"] == f"LIVE_{name}"
                        ):
                            assert frame["params"]["threadId"] == child_id
                            break
                    await child.send(*stream.events[len(prefix.events) :])
                    await child.close()
                    async with await openai_responses.await_next_request() as continued:
                        assert continued.request.client_metadata.thread_id == child_id
                        result = continued.request.function_call_outputs[-1]
                        assert result.call_id == f"shell_{name}"
                        assert f"OUT_{name}" in result.output
                        assert f"ERR_{name}" in result.output
                        assert "23" in result.output
                        await continued.send(
                            *responses_sse.response_stream([responses_sse.Message(f"DONE_{name}")], model=MODEL).events
                        )
                    while True:
                        snapshot = (await rpc(process, "thread/read", threadId=child_id, includeTurns=True))["thread"]
                        if snapshot["status"]["type"] == "idle":
                            break
                        await asyncio.sleep(0.05)
                    assert snapshot["turns"][-1]["status"] == "completed"
                    assert f"DONE_{name}" in json.dumps(snapshot["turns"])
                    if name == "second":
                        sibling = (await rpc(process, "thread/read", threadId=child_ids["first"], includeTurns=False))[
                            "thread"
                        ]
                        assert sibling["status"]["type"] == "active"
                captured = process.stdout_frames()
                for name, child_id in child_ids.items():
                    (spawn,) = [
                        frame["params"]
                        for frame in captured
                        if frame.get("method") == "item/completed" and frame["params"]["item"]["id"] == f"spawn_{name}"
                    ]
                    assert spawn["threadId"] == root_id
                    if v2:
                        assert spawn["item"]["type"] == "subAgentActivity"
                        assert spawn["item"]["agentThreadId"] == child_id
                        assert spawn["item"]["agentPath"] == f"/root/{name}"
                    else:
                        assert spawn["item"]["type"] == "collabAgentToolCall"
                        assert spawn["item"]["senderThreadId"] == root_id
                        assert spawn["item"]["receiverThreadIds"] == [child_id]
                    items = [
                        frame["params"]["item"]
                        for frame in captured
                        if frame.get("method") == "item/completed" and frame["params"]["threadId"] == child_id
                    ]
                    completed_items[child_id] = items
                    assert len({item["id"] for item in items}) == len(items)
                    assert [item["summary"] for item in items if item["type"] == "reasoning"] == [[f"THINK_{name}"]]
                    shell = [item for item in items if item["type"] == "commandExecution"]
                    assert len(shell) == 1
                    assert shell[0]["exitCode"] == 23
                    assert f"OUT_{name}" in shell[0]["aggregatedOutput"]
                    assert f"ERR_{name}" in shell[0]["aggregatedOutput"]
                    assert [item["text"] for item in items if item["type"] == "agentMessage"] == [
                        f"LIVE_{name}",
                        f"DONE_{name}",
                    ]
                assert await process.crash() < 0
                await parent.wait_client_closed()
        async with server(codex, openai_responses, "recovered-siblings") as recovered:
            assert await listed(recovered, "thread/loaded/list") == []
            for name, child_id in child_ids.items():
                snapshot = (await rpc(recovered, "thread/read", threadId=child_id, includeTurns=True))["thread"]
                assert snapshot["id"] == child_id
                assert snapshot["status"]["type"] == "notLoaded"
                assert snapshot["turns"][-1]["status"] == "completed"
                assert [item["id"] for item in snapshot["turns"][-1]["items"]] == [
                    item["id"] for item in completed_items[child_id]
                ]
                transcript = json.dumps(snapshot["turns"])
                for marker in (f"LIVE_{name}", f"DONE_{name}", f"OUT_{name}", f"ERR_{name}"):
                    assert marker in transcript
                other = "second" if name == "first" else "first"
                assert f"DONE_{other}" not in transcript
            assert await listed(recovered, "thread/loaded/list") == []
            with pytest.raises(TimeoutError):
                async with asyncio.timeout(0.25):
                    await openai_responses.await_next_request()


@pytest.mark.parametrize("v2", [False, True], ids=["v1", "v2"])
@pytest.mark.parametrize("ending", ["crash-active", "model-error"])
async def test_nested_child_ancestry_and_terminal_recovery(
    codex: CodexHarness, openai_responses: OpenAIResponses, v2: bool, ending: str
) -> None:
    """V1 grandchildren and v2's fresh-child delegation boundary, through failure and crash."""
    namespace = "collaboration" if v2 else "multi_agent_v1"
    ancestry: dict[str, str] = {}
    async with asyncio.timeout(90):
        async with server(codex, openai_responses, "nested") as process:
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
                        "features.multi_agent_v2": v2,
                        "agents.max_depth": 2,
                    },
                )
            )["thread"]
            root_id = root["id"]
            await rpc(
                process, "turn/start", threadId=root_id, input=[{"type": "text", "text": "Delegate a nested probe."}]
            )
            async with AsyncExitStack() as exchanges:
                current = await exchanges.enter_async_context(await openai_responses.await_next_request())
                held_parents = []
                for name in ("child",) if v2 else ("child", "grandchild"):
                    parent_id = current.request.client_metadata.thread_id
                    assert namespace in current.request.tool_names
                    arguments: dict[str, Any] = {"message": f"NESTED_{name}"}
                    if v2:
                        arguments.update(task_name=name, fork_turns="none")
                    else:
                        arguments["fork_context"] = False
                    await current.send(
                        *responses_sse.response_stream(
                            [
                                responses_sse.FunctionCall(
                                    f"spawn_{name}", "spawn_agent", arguments, namespace=namespace
                                )
                            ],
                            model=MODEL,
                        ).events
                    )
                    await current.close()
                    first = await exchanges.enter_async_context(await openai_responses.await_next_request())
                    second = await exchanges.enter_async_context(await openai_responses.await_next_request())
                    parent, current = (
                        (first, second) if first.request.client_metadata.thread_id == parent_id else (second, first)
                    )
                    assert parent.request.client_metadata.thread_id == parent_id
                    assert parent.request.function_call_outputs[-1].call_id == f"spawn_{name}"
                    held_parents.append(parent)
                    child_id = current.request.client_metadata.thread_id
                    assert child_id not in {root_id, *ancestry}
                    assert f"NESTED_{name}" in current.request.model_dump_json()
                    ancestry[child_id] = parent_id
                    snapshot = (await rpc(process, "thread/read", threadId=child_id, includeTurns=False))["thread"]
                    assert snapshot["parentThreadId"] == parent_id
                    assert snapshot["status"]["type"] == "active"
                assert namespace not in current.request.tool_names
                leaf_id = current.request.client_metadata.thread_id
                if ending == "model-error":
                    observer = process.frames()
                    await current.abort()
                    for _ in range(scenarios.MAX_RETRIES):
                        async with await openai_responses.await_next_request() as retry:
                            assert retry.request.client_metadata.thread_id == leaf_id
                            await retry.abort()
                    while True:
                        frame = await observer.next()
                        if frame.get("method") == "turn/completed" and frame["params"]["threadId"] == leaf_id:
                            assert frame["params"]["turn"]["status"] == "failed"
                            assert frame["params"]["turn"]["error"] is not None
                            break
                    errors = [
                        frame["params"]
                        for frame in process.stdout_frames()
                        if frame.get("method") == "error" and frame["params"]["threadId"] == leaf_id
                    ]
                    assert [error["willRetry"] for error in errors] == [True, True, False]
                    for ancestor_id in {root_id, ancestry[leaf_id]}:
                        ancestor = (await rpc(process, "thread/read", threadId=ancestor_id, includeTurns=False))[
                            "thread"
                        ]
                        assert ancestor["status"]["type"] == "active"
                assert await process.crash() < 0
                for parent in held_parents:
                    await parent.wait_client_closed()
                if ending == "crash-active":
                    await current.wait_client_closed()
        async with server(codex, openai_responses, "nested-recovery") as recovered:
            for child_id, parent_id in ancestry.items():
                snapshot = (await rpc(recovered, "thread/read", threadId=child_id, includeTurns=True))["thread"]
                assert snapshot["id"] == child_id
                assert snapshot["parentThreadId"] == parent_id
                assert snapshot["status"]["type"] == "notLoaded"
                expected = "failed" if ending == "model-error" and child_id == leaf_id else "interrupted"
                assert snapshot["turns"][-1]["status"] == expected
                descendants = await listed(recovered, "thread/list", parentThreadId=parent_id, modelProviders=[])
                assert child_id in {thread["id"] for thread in descendants}
            assert await listed(recovered, "thread/loaded/list") == []
            with pytest.raises(TimeoutError):
                async with asyncio.timeout(0.25):
                    await openai_responses.await_next_request()


if __name__ == "__main__":
    pytest_bazel.main()
