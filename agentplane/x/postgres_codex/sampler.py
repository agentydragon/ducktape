"""One durable sampling or code-cell transition; no rollout files or Runner journal."""

from typing import Any

from aiohttp import ClientSession

from agentplane.x.postgres_codex.code_mode import CodeMode
from agentplane.x.postgres_codex.compaction import compact
from agentplane.x.postgres_codex.responses import request_body, sample
from agentplane.x.postgres_codex.store import KnownFailureError, Owner, Store, UnknownEffectError


def pending_call(items: list[dict[str, Any]]) -> dict[str, Any] | None:
    completed = {item["call_id"] for item in items if item["type"] == "custom_tool_call_output"}
    return next(
        (item for item in items if item["type"] == "custom_tool_call" and item["call_id"] not in completed), None
    )


async def advance(
    store: Store,
    owner: Owner,
    runtime: CodeMode | None,
    http: ClientSession,
    *,
    endpoint: str,
    credential: str,
    model: str,
    auto_compact_tokens: int | None = None,
) -> str:
    if auto_compact_tokens is not None and auto_compact_tokens <= 0:
        raise ValueError("auto-compaction threshold must be positive")
    row = await store.read(owner)
    if row.pending is not None:
        raise UnknownEffectError(f"unresolved {row.pending['kind']} intent {row.pending['id']}; no automatic replay")
    if row.stopped:
        return "interrupted"
    await store.check_live_cells(owner, runtime_available=runtime is not None)
    execution_state = await store.execution_state(owner)
    if runtime is not None and runtime.error is not None:
        raise runtime.error
    if (call := pending_call(row.items)) is not None:
        if call["name"] not in {"exec", "wait"}:
            raise ValueError(f"unsupported model tool: {call['name']}")
        if runtime is None:
            raise ValueError("a pending cell requires a code-mode runtime")
        intent_id = await store.begin(owner, {"kind": "cell", "call": call})
        try:
            output = await runtime.execute(call)
        except KnownFailureError as failure:
            await store.reject(owner, intent_id, failure)
            raise
        result = {"type": "custom_tool_call_output", "call_id": call["call_id"], "output": output}
        await store.finish(owner, intent_id, result, [result])
        return "cell"
    if row.items[-1]["type"] == "message" and row.items[-1]["role"] == "assistant":
        return "done"
    if (
        auto_compact_tokens is not None
        and row.context_tokens is not None
        and row.context_tokens >= auto_compact_tokens
        and not execution_state["cells"]
    ):
        await compact(store, owner, http, endpoint=endpoint, credential=credential, model=model)
        return "compact"
    body = request_body(
        model, str(row.id), row.context(), instructions=row.instructions, execution_state=execution_state
    )
    intent_id = await store.begin(owner, {"kind": "sampling", "body": body})
    try:
        sampled = await sample(http, endpoint, credential, body)
        for item in sampled.output:
            if item["type"] not in {"message", "reasoning", "custom_tool_call"}:
                raise KnownFailureError({"reason": "unsupported provider output type", **sampled.audit()})
            if item["type"] == "custom_tool_call" and item["name"] not in {"exec", "wait"}:
                raise KnownFailureError({"reason": "unsupported model tool", **sampled.audit()})
    except KnownFailureError as failure:
        await store.reject(owner, intent_id, failure)
        raise
    # Provider items, including opaque reasoning and unknown fields, remain intact in JSONB.
    usage = sampled.terminal.get("usage")
    await store.finish(
        owner,
        intent_id,
        sampled.audit(),
        sampled.output,
        context_tokens=None if usage is None else usage.get("total_tokens"),
    )
    return "sample"
