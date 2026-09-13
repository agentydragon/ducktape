"""Async test-driving steps for the native Codex app-server protocol."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from x.agentplane.native.async_process import AsyncNativeProcess
from x.agentplane.native.codex import driver
from x.agentplane.native.codex.scenarios import MAX_RETRIES


async def _next_matching(process: AsyncNativeProcess, predicate: Callable[[dict[str, Any]], bool]) -> dict[str, Any]:
    while True:
        frame = await process.next_frame()
        if predicate(frame):
            return frame


async def launch_handshake(
    process: AsyncNativeProcess, *, cwd: str, model: str, effort: str, persist: bool = False, config: dict[str, Any] | None = None
) -> dict[str, Any]:
    initialize = driver.initialize("capture-1")
    await process.send(initialize)
    init_response = await _next_matching(process, lambda item: item.get("id") == initialize.id)
    await process.send(driver.initialized())
    start = driver.thread_start("capture-2", cwd=cwd, model=model, effort=effort, persist=persist, config=config)
    await process.send(start)
    started = await _next_matching(process, lambda item: item.get("id") == "capture-2")
    return {"initialize_response": init_response, "thread_start_response": started, "thread_id": _thread_id(started)}


async def resume_handshake(process: AsyncNativeProcess, *, thread_id: str) -> dict[str, Any]:
    initialize = driver.initialize("capture-4")
    await process.send(initialize)
    init_response = await _next_matching(process, lambda item: item.get("id") == initialize.id)
    await process.send(driver.initialized())
    resume = driver.thread_resume("capture-5", thread_id=thread_id)
    await process.send(resume)
    resumed = await _next_matching(process, lambda item: item.get("id") == "capture-5")
    return {"initialize_response": init_response, "thread_resume_response": resumed, "thread_id": thread_id}


async def start_turn(
    process: AsyncNativeProcess, *, thread_id: str, request_id: str, text: str, model: str | None = None
) -> str:
    await process.send(driver.turn_start(request_id, thread_id=thread_id, text=text, model=model))
    started = await _next_matching(process, lambda item: item.get("id") == request_id)
    result = started.get("result")
    turn = result.get("turn") if isinstance(result, dict) else None
    turn_id = turn.get("id") if isinstance(turn, dict) else None
    if not isinstance(turn_id, str):
        raise ValueError(f"Codex turn/start did not return a turn id: {started}")
    return turn_id


async def await_turn_started(process: AsyncNativeProcess) -> dict[str, Any]:
    return await _next_matching(process, lambda item: item.get("method") == "turn/started")


async def await_error(process: AsyncNativeProcess) -> dict[str, Any]:
    return await _next_matching(process, lambda item: item.get("method") == "error" and isinstance(item.get("params"), dict))


async def await_turn_completed(process: AsyncNativeProcess) -> dict[str, Any]:
    return await _next_matching(
        process, lambda item: item.get("method") == "turn/completed" and isinstance(item.get("params"), dict)
    )


async def steer(
    process: AsyncNativeProcess, *, thread_id: str, turn_id: str, request_id: str, text: str
) -> dict[str, Any]:
    await process.send(driver.steer(request_id, thread_id=thread_id, turn_id=turn_id, text=text))
    return await _next_matching(process, lambda item: item.get("id") == request_id)


async def interrupt(process: AsyncNativeProcess, *, thread_id: str, turn_id: str, request_id: str) -> dict[str, Any]:
    await process.send(driver.interrupt(request_id, thread_id=thread_id, turn_id=turn_id))
    return await _next_matching(process, lambda item: item.get("id") == request_id)


def _thread_id(started: dict[str, Any]) -> str:
    result = started.get("result")
    thread = result.get("thread") if isinstance(result, dict) else None
    thread_id = thread.get("id") if isinstance(thread, dict) else None
    if not isinstance(thread_id, str):
        raise ValueError("Codex thread/start did not return a durable thread id")
    return thread_id
