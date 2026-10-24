"""Compare complete app-server history views with actual resumed model input."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from uuid import uuid4

import pytest
import pytest_bazel
from pydantic import BaseModel

from agentplane.native.codex import driver, scenarios
from agentplane.native.codex.testing import responses_sse
from agentplane.native.codex.testing.harness import CodexHarness
from agentplane.native.codex.testing.model import EFFORT, MODEL
from agentplane.native.codex.testing.responses import OpenAIResponses
from agentplane.native.testing.async_process import AsyncNativeProcess, FrameCursor


class HistoryRequest(BaseModel):
    id: str
    method: str
    params: dict[str, Any]


async def request(process: AsyncNativeProcess, method: str, **params: Any) -> dict[str, Any]:
    message = HistoryRequest(id=str(uuid4()), method=method, params=params)
    return (await process.request(message, matches=lambda frame: frame.get("id") == message.id)).frame


async def rpc(process: AsyncNativeProcess, method: str, **params: Any) -> dict[str, Any]:
    frame = await request(process, method, **params)
    assert "error" not in frame, frame
    result = frame["result"]
    assert isinstance(result, dict)
    return result


@asynccontextmanager
async def server(codex: CodexHarness, model: OpenAIResponses, name: str) -> AsyncIterator[AsyncNativeProcess]:
    endpoint = f"{model.origin}/v1"
    logs = codex.logs / name
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
            clientInfo={"name": "agentplane-history-probe", "version": "0.1"},
            capabilities={"experimentalApi": True},
        )
        await process.send(driver.initialized())
        yield process


async def history(process: AsyncNativeProcess, thread_id: str) -> dict[str, Any]:
    result: dict[str, Any] = {
        "thread/read": await request(process, "thread/read", threadId=thread_id, includeTurns=True)
    }
    for method, options in (
        ("thread/turns/list", {"itemsView": "full"}),
        ("thread/items/list", {}),
        ("thread/timeline/list", {}),
    ):
        pages = []
        cursor = None
        while True:
            page = await request(process, method, threadId=thread_id, limit=1, cursor=cursor, **options)
            pages.append(page)
            cursor = page.get("result", {}).get("nextCursor")
            if cursor is None:
                break
        result[method] = pages
    return result


async def completed(cursor: FrameCursor, turn_id: str) -> None:
    while True:
        frame = await cursor.next()
        if frame.get("method") == "turn/completed" and frame["params"]["turn"]["id"] == turn_id:
            assert frame["params"]["turn"]["status"] == "completed"
            return


def assert_history_views(evidence: dict[str, Any], *, legacy: bool) -> None:
    announced = evidence["announced"]
    for phase in ("loaded", "cold", "resumed"):
        views = evidence[phase]
        read_items = [item for turn in views["thread/read"]["result"]["thread"]["turns"] for item in turn["items"]]
        turn_items = [
            item for page in views["thread/turns/list"] for turn in page["result"]["data"] for item in turn["items"]
        ]
        all_views = [read_items, turn_items]
        if legacy:
            for method in ("thread/items/list", "thread/timeline/list"):
                (page,) = views[method]
                assert page["error"] == {"code": -32601, "message": f"{method} is not supported yet"}
        else:
            all_views.extend(
                [entry["item"] for page in views[method] for entry in page["result"]["data"] if "item" in entry]
                for method in ("thread/items/list", "thread/timeline/list")
            )
        for items in all_views:
            if legacy:
                assert [item["type"] for item in items] == ["userMessage", "reasoning", "agentMessage"]
                assert items[1]["summary"] == ["first thought", "second thought"]
                assert items[1]["id"] not in {item["id"] for item in announced}
            else:
                assert {(item["type"], item["id"]) for item in items} == {
                    (item["type"], item["id"]) for item in announced
                }
                assert {item["id"]: item["summary"] for item in items if item["type"] == "reasoning"} == {
                    item["id"]: item["summary"] for item in announced if item["type"] == "reasoning"
                }
                (command,) = [item for item in items if item["type"] == "commandExecution"]
                assert command["status"] == "completed"
                assert command["aggregatedOutput"] == "HISTORY_TOOL"
    assert evidence["resume"]["thread"]["turns"] == evidence["resumed"]["thread/read"]["result"]["thread"]["turns"]
    saved_reasoning = {
        record["payload"]["id"]: [part["text"] for part in record["payload"]["summary"]]
        for record in evidence["rollout"]
        if record["type"] == "response_item" and record["payload"]["type"] == "reasoning"
    }
    assert saved_reasoning == {item["id"]: item["summary"] for item in announced if item["type"] == "reasoning"}


@pytest.mark.parametrize("history_mode", [None, "legacy"])
async def test_history_projection_against_resumed_model_input(
    codex: CodexHarness, openai_responses: OpenAIResponses, history_mode: str | None
) -> None:
    evidence: dict[str, Any] = {"requestedHistoryMode": history_mode}
    async with asyncio.timeout(60):
        async with server(codex, openai_responses, "initial") as process:
            thread = (
                await rpc(
                    process,
                    "thread/start",
                    cwd=str(codex.workspace),
                    model=MODEL,
                    approvalPolicy="never",
                    sandbox="danger-full-access",
                    ephemeral=False,
                    baseInstructions=scenarios.BASE_INSTRUCTIONS,
                    config={"model_reasoning_effort": EFFORT},
                    **({"historyMode": history_mode} if history_mode else {}),
                )
            )["thread"]
            assert thread["historyMode"] == (history_mode or "paginated")
            evidence["thread"] = thread
            thread_id = thread["id"]
            cursor = process.frames()
            turn = (await rpc(process, "turn/start", threadId=thread_id, input=[{"type": "text", "text": "Seed"}]))[
                "turn"
            ]
            async with await openai_responses.await_next_request() as exchange:
                await exchange.send(
                    *responses_sse.response_stream(
                        [
                            responses_sse.Reasoning("first thought", "enc_history_1"),
                            responses_sse.FunctionCall("call_history", "exec_command", {"cmd": "printf HISTORY_TOOL"}),
                        ],
                        model=MODEL,
                    ).events
                )
            async with await openai_responses.await_next_request() as exchange:
                assert "HISTORY_TOOL" in exchange.request.function_call_outputs[0].output
                await exchange.send(
                    *responses_sse.response_stream(
                        [responses_sse.Reasoning("second thought", "enc_history_2"), responses_sse.Message("DONE")],
                        model=MODEL,
                    ).events
                )
            await completed(cursor, turn["id"])
            evidence["announced"] = [
                frame["params"]["item"] for frame in process.stdout_frames() if frame.get("method") == "item/completed"
            ]
            evidence["loaded"] = await history(process, thread_id)
        evidence["rollout"] = [
            json.loads(line)
            for path in (codex.codex_home / "sessions").glob(f"**/*{thread_id}.jsonl")
            for line in path.read_text().splitlines()
        ]
        async with server(codex, openai_responses, "restarted") as process:
            evidence["cold"] = await history(process, thread_id)
            evidence["resume"] = await rpc(process, "thread/resume", threadId=thread_id, excludeTurns=False)
            evidence["resumed"] = await history(process, thread_id)
            cursor = process.frames()
            turn = (await rpc(process, "turn/start", threadId=thread_id, input=[{"type": "text", "text": "Continue"}]))[
                "turn"
            ]
            async with await openai_responses.await_next_request() as exchange:
                evidence["modelRequest"] = exchange.request.model_dump(mode="json")
                (codex.logs / "history-comparison.json").write_text(json.dumps(evidence, indent=2) + "\n")
                assert_history_views(evidence, legacy=history_mode == "legacy")
                assert [item.encrypted_content for item in exchange.request.reasoning] == [
                    "enc_history_1",
                    "enc_history_2",
                ]
                assert [[part.text for part in item.summary] for item in exchange.request.reasoning] == [
                    ["first thought"],
                    ["second thought"],
                ]
                assert [item.call_id for item in exchange.request.function_calls] == ["call_history"]
                assert "HISTORY_TOOL" in exchange.request.function_call_outputs[0].output
                await exchange.send(
                    *responses_sse.response_stream([responses_sse.Message("CONTINUED")], model=MODEL).events
                )
            await completed(cursor, turn["id"])


if __name__ == "__main__":
    pytest_bazel.main()
