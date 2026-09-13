"""Async test-driving steps for the native Claude stream-json protocol."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from x.agentplane.native.async_process import AsyncNativeProcess
from x.agentplane.native.claude import driver
from x.agentplane.native.claude.scenarios import HOOK_EVENTS, MAX_RETRIES, SYSTEM_PROMPT, TOOLS, session_id


async def _next_matching(process: AsyncNativeProcess, predicate: Callable[[dict[str, Any]], bool]) -> dict[str, Any]:
    while True:
        frame = await process.next_frame()
        if predicate(frame):
            return frame


async def launch_handshake(process: AsyncNativeProcess, *, hooks: bool = False) -> dict[str, Any]:
    frame = driver.initialize(hooks={event: [f"capture-{event}"] for event in HOOK_EVENTS} if hooks else None)
    await process.send(frame)
    reply = await _next_matching(
        process,
        lambda item: (
            item.get("type") == "control_response"
            and isinstance(item.get("response"), dict)
            and item["response"].get("request_id") == frame.request_id
        ),
    )
    return {"initialize_request_id": frame.request_id, "control_response": reply}


async def send(process: AsyncNativeProcess, text: str) -> str:
    frame = driver.user_frame(text)
    await process.send(frame)
    return frame.uuid


async def await_result(process: AsyncNativeProcess) -> dict[str, Any]:
    return await _next_matching(process, lambda item: item.get("type") == "result")


async def await_active(process: AsyncNativeProcess) -> dict[str, Any]:
    return await _next_matching(
        process,
        lambda item: (
            item.get("type") == "stream_event"
            or (
                item.get("type") == "assistant"
                and any(block.get("type") == "tool_use" for block in item.get("message", {}).get("content", []))
            )
        ),
    )


async def interrupt(process: AsyncNativeProcess, *, cancel_queued: bool) -> dict[str, Any]:
    request = driver.interrupt(cancel_queued=cancel_queued)
    await process.send(request)
    return await _next_matching(
        process,
        lambda item: (
            item.get("type") == "control_response" and item.get("response", {}).get("request_id") == request.request_id
        ),
    )
