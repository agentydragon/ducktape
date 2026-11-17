"""Round-trip and persistence evidence for Codex turn/start client message IDs."""

from __future__ import annotations

import asyncio
from typing import Any

from agentplane.native.codex import scenarios
from agentplane.native.codex.testing import frames, responses_sse
from agentplane.native.codex.testing.harness import CodexHarness
from agentplane.native.codex.testing.model import EFFORT, MODEL
from agentplane.native.codex.testing.probe_support import rpc, server
from agentplane.native.codex.testing.responses import OpenAIResponses
from agentplane.native.testing.async_process import FrameCursor

TOOL_COMMAND = 'sh -c \'printf "wait_started\\n"; sleep 3; printf "wait_finished\\n"\''
FIRST_TEXT = "Wait while the shell command runs."
JOINED_TEXT = "Reply with exactly: CLIENT_MESSAGE_JOINED_OK"
INTERRUPTED_TEXT = "Wait for recovery; do not answer early."
QUEUED_TEXT = "Reply with exactly: CLIENT_MESSAGE_DROPPED_QUEUE"
RECOVERY_TEXT = "Reply with exactly: CLIENT_MESSAGE_INTERRUPT_RECOVERY_OK"
FIRST_CLIENT_ID = "client-first-turn"
JOINED_CLIENT_ID = "client-joined-turn"
INTERRUPTED_CLIENT_ID = "client-interrupted-turn"
QUEUED_CLIENT_ID = "client-interrupted-joined-input"
RECOVERY_CLIENT_ID = "client-interrupt-recovery"


async def _start_persisted_thread(process: Any, codex: CodexHarness) -> str:
    result = await rpc(
        process,
        "thread/start",
        cwd=str(codex.workspace),
        model=MODEL,
        approvalPolicy="never",
        sandbox="danger-full-access",
        ephemeral=False,
        baseInstructions=scenarios.BASE_INSTRUCTIONS,
        config={"model_reasoning_effort": EFFORT},
    )
    thread_id = result["thread"]["id"]
    assert isinstance(thread_id, str)
    return thread_id


async def _start_turn(process: Any, thread_id: str, text: str, client_id: str) -> dict[str, Any]:
    result = await rpc(
        process, "turn/start", threadId=thread_id, clientUserMessageId=client_id, input=[{"type": "text", "text": text}]
    )
    turn = result["turn"]
    assert isinstance(turn, dict)
    return turn


async def _user_message_lifecycle(cursor: FrameCursor, client_id: str, text: str) -> None:
    lifecycle: list[dict[str, Any]] = []
    async with asyncio.timeout(20):
        while len(lifecycle) < 2:
            frame = await cursor.next()
            if frame.get("method") not in {"item/started", "item/completed"}:
                continue
            item = frame["params"]["item"]
            if item.get("type") == "userMessage" and item.get("clientId") == client_id:
                lifecycle.append(frame)
    assert [frame["method"] for frame in lifecycle] == ["item/started", "item/completed"]
    assert [frame["params"]["item"]["content"][0]["text"] for frame in lifecycle] == [text, text]


async def _command_started(cursor: FrameCursor) -> None:
    while True:
        frame = await cursor.next()
        if (
            frame.get("method") == "item/started"
            and frame.get("params", {}).get("item", {}).get("type") == "commandExecution"
        ):
            return


async def _turn_completed(cursor: FrameCursor, turn_id: str, status: str) -> None:
    while True:
        frame = await cursor.next()
        if frame.get("method") == "turn/completed" and frame.get("params", {}).get("turn", {}).get("id") == turn_id:
            assert frame["params"]["turn"]["status"] == status
            return


async def _list_all(process: Any, method: str, **params: Any) -> list[dict[str, Any]]:
    records = []
    cursor = None
    while True:
        page = await rpc(process, method, cursor=cursor, limit=100, **params)
        records.extend(page["data"])
        cursor = page.get("nextCursor")
        if cursor is None:
            return records


def _history_views(
    read: dict[str, Any], turns: list[dict[str, Any]], item_rows: list[dict[str, Any]]
) -> dict[str, list[dict[str, Any]]]:
    return {
        "thread/read": [item for turn in read["turns"] for item in turn["items"]],
        "thread/turns/list": [item for turn in turns for item in turn["items"]],
        "thread/items/list": [row["item"] for row in item_rows if "item" in row],
    }


async def test_turn_start_client_message_id_round_trips_through_join_and_resumed_history(
    codex: CodexHarness, openai_responses: OpenAIResponses
) -> None:
    async with asyncio.timeout(120):
        async with server(codex, openai_responses, "client-id-initial", experimental_api=False) as process:
            thread_id = await _start_persisted_thread(process, codex)
            events = process.frames()
            first = await _start_turn(process, thread_id, FIRST_TEXT, FIRST_CLIENT_ID)

            async with await openai_responses.await_next_request() as exchange:
                assert exchange.request.messages("user")[-1].text == FIRST_TEXT
                await _user_message_lifecycle(events, FIRST_CLIENT_ID, FIRST_TEXT)
                await exchange.send(
                    *responses_sse.response_stream(
                        [responses_sse.FunctionCall("call_client_id_wait", "exec_command", {"cmd": TOOL_COMMAND})],
                        model=MODEL,
                    ).events
                )

            await _command_started(events)
            joined = await _start_turn(process, thread_id, JOINED_TEXT, JOINED_CLIENT_ID)
            assert joined["id"] == first["id"]

            async with await openai_responses.await_next_request() as exchange:
                assert exchange.request.messages("user")[-1].text == JOINED_TEXT
                await _user_message_lifecycle(events, JOINED_CLIENT_ID, JOINED_TEXT)
                await exchange.send(
                    *responses_sse.response_stream(
                        [responses_sse.Message("CLIENT_MESSAGE_JOINED_OK")], model=MODEL
                    ).events
                )
            await _turn_completed(events, first["id"], "completed")

        async with server(codex, openai_responses, "client-id-resumed", experimental_api=False) as resumed:
            restored = await rpc(resumed, "thread/resume", threadId=thread_id, excludeTurns=False)
            assert restored["thread"]["id"] == thread_id
            assert restored["thread"]["historyMode"] == "paginated"
            read = (await rpc(resumed, "thread/read", threadId=thread_id, includeTurns=True))["thread"]
            turns = await _list_all(resumed, "thread/turns/list", threadId=thread_id, itemsView="full")
            item_rows = await _list_all(resumed, "thread/items/list", threadId=thread_id)
            for view_name, items in _history_views(read, turns, item_rows).items():
                for client_id, text in ((FIRST_CLIENT_ID, FIRST_TEXT), (JOINED_CLIENT_ID, JOINED_TEXT)):
                    matches = [
                        item
                        for item in items
                        if item.get("type") == "userMessage" and item.get("clientId") == client_id
                    ]
                    assert matches, f"{client_id} missing from {view_name}"
                    assert all(item["content"][0]["text"] == text for item in matches), view_name


def _items_from_wire(frames_out: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        frame["params"]["item"]
        for frame in frames_out
        if frame.get("method") in {"item/started", "item/completed"} and "item" in frame.get("params", {})
    ]


async def test_interrupt_drops_joined_client_message_before_it_reaches_history(
    codex: CodexHarness, openai_responses: OpenAIResponses
) -> None:
    async with asyncio.timeout(120):
        async with server(codex, openai_responses, "client-id-interrupted", experimental_api=False) as process:
            thread_id = await _start_persisted_thread(process, codex)
            events = process.frames()
            initial = await _start_turn(process, thread_id, INTERRUPTED_TEXT, INTERRUPTED_CLIENT_ID)

            async with await openai_responses.await_next_request() as exchange:
                await _user_message_lifecycle(events, INTERRUPTED_CLIENT_ID, INTERRUPTED_TEXT)
                queued = await _start_turn(process, thread_id, QUEUED_TEXT, QUEUED_CLIENT_ID)
                assert queued["id"] == initial["id"]
                response = await rpc(process, "turn/interrupt", threadId=thread_id, turnId=initial["id"])
                assert "error" not in response
                await exchange.wait_client_closed()
            await _turn_completed(events, initial["id"], "interrupted")
            assert all(item.get("clientId") != QUEUED_CLIENT_ID for item in _items_from_wire(process.stdout_frames()))

            recovery = await _start_turn(process, thread_id, RECOVERY_TEXT, RECOVERY_CLIENT_ID)
            async with await openai_responses.await_next_request() as exchange:
                recovery_user_text = [message.text for message in exchange.request.messages("user")]
                assert QUEUED_TEXT not in recovery_user_text
                assert frames.INTERRUPTED_TURN_MARKER in recovery_user_text
                await _user_message_lifecycle(events, RECOVERY_CLIENT_ID, RECOVERY_TEXT)
                await exchange.send(
                    *responses_sse.response_stream(
                        [responses_sse.Message("CLIENT_MESSAGE_INTERRUPT_RECOVERY_OK")], model=MODEL
                    ).events
                )
            await _turn_completed(events, recovery["id"], "completed")

        async with server(codex, openai_responses, "client-id-interrupted-resumed", experimental_api=False) as resumed:
            restored = await rpc(resumed, "thread/resume", threadId=thread_id, excludeTurns=False)
            assert restored["thread"]["historyMode"] == "paginated"
            read = (await rpc(resumed, "thread/read", threadId=thread_id, includeTurns=True))["thread"]
            turns = await _list_all(resumed, "thread/turns/list", threadId=thread_id, itemsView="full")
            item_rows = await _list_all(resumed, "thread/items/list", threadId=thread_id)
            for view_name, items in _history_views(read, turns, item_rows).items():
                history_json = str(items)
                assert QUEUED_CLIENT_ID not in history_json, view_name
                assert QUEUED_TEXT not in history_json, view_name
