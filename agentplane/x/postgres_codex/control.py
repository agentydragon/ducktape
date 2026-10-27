"""External, generation-targeted interrupt requests observed during active transitions."""

import asyncio
from typing import Any

from aiohttp import ClientSession

from agentplane.x.postgres_codex.code_mode import CodeMode
from agentplane.x.postgres_codex.executor import Executor
from agentplane.x.postgres_codex.sampler import advance
from agentplane.x.postgres_codex.store import Owner, Store


async def controlled_advance(
    store: Store,
    owner: Owner,
    runtime: CodeMode | None,
    executor: Executor | None,
    http: ClientSession,
    *,
    endpoint: str,
    credential: str,
    model: str,
    auto_compact_tokens: int | None = None,
    reasoning_effort: str | None = None,
) -> str:
    task: asyncio.Task[str] | None = None
    try:
        while True:
            requests = await store.pending_interrupts(owner)
            if requests:
                # Do not cancel delegate RPCs: termination must consume their replies before
                # reusing the connection. Cancelling sampling closes its HTTP response.
                row = await store.read(owner)
                if executor is not None:
                    executor.stopping = True
                if task is not None:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                cleanup_error = None
                cells: dict[str, Any] = {}
                processes: dict[str, Any] = {}
                try:
                    async with asyncio.timeout(15):
                        cells = await runtime.interrupt() if runtime is not None else {}
                        processes = await executor.interrupt_all() if executor is not None else {}
                except (ConnectionError, RuntimeError, TimeoutError) as error:
                    cleanup_error = str(error)
                delegates_stopped = True
                if runtime is not None and runtime.delegates:
                    _, pending = await asyncio.wait(runtime.delegates, timeout=5)
                    delegates_stopped = not pending
                state = await store.execution_state(owner)
                complete = (
                    not state["cells"]
                    and not any(process["status"] != "exited_unread" for process in state["processes"])
                    and delegates_stopped
                    and cleanup_error is None
                )
                result: dict[str, Any] = {
                    "status": "interrupted" if complete else "effects_unknown",
                    "provider_cancelled": row.pending is not None and row.pending["kind"] in {"sampling", "compaction"},
                    "provider_completion_unknown": row.pending is not None
                    and row.pending["kind"] in {"sampling", "compaction"},
                    "cells": cells,
                    "processes": processes,
                    "cleanup_complete": complete,
                    "partial_effects_possible": True,
                    "cleanup_error": cleanup_error,
                }
                await store.acknowledge_interrupt(owner, requests, result)
                return "interrupted" if complete else "effects_unknown"
            if task is None:
                task = asyncio.create_task(
                    advance(
                        store,
                        owner,
                        runtime,
                        http,
                        endpoint=endpoint,
                        credential=credential,
                        model=model,
                        auto_compact_tokens=auto_compact_tokens,
                        reasoning_effort=reasoning_effort,
                    )
                )
            if task.done():
                return await task
            await asyncio.wait({task}, timeout=0.05)
    finally:
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
