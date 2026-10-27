"""Responses Lite request assembly and terminal streaming transport."""

import json
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from aiohttp import ClientSession

from agentplane.x.postgres_codex.code_mode import tool_definitions
from agentplane.x.postgres_codex.store import KnownFailureError, Owner, Store


def request_body(
    model: str,
    conversation_id: str,
    items: list[dict[str, Any]],
    *,
    instructions: str | None = None,
    execution_state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    description = (
        "Run raw JavaScript as an async module in a fresh V8 isolate. Call tools.exec_command({cmd, workdir?}) "
        "or tools.apply_patch(patch). Emit results with text(value). No Node, filesystem, network or console. "
        "exec_command supports tty and yield_time_ms; a running process returns session_id. "
        "Use tools.write_stdin({session_id, chars?, eof?, yield_time_ms?}) to send input or poll. "
        'An exec first-line // @exec: {"yield_time_ms": 10} yields an active JS cell; '
        "call wait with JSON {cell_id, yield_time_ms?, terminate?} to observe it. "
        "Process session_id and JavaScript cell_id are distinct. No notify or persistent JS store. Await all tool calls. "
        + json.dumps(tool_definitions())
    )
    return {
        "model": model,
        "input": [
            {
                "type": "additional_tools",
                "role": "developer",
                "tools": [
                    {"type": "custom", "name": "exec", "description": description, "format": {"type": "text"}},
                    {
                        "type": "custom",
                        "name": "wait",
                        "description": "Observe a yielded JavaScript cell. Input JSON: {cell_id, yield_time_ms?, terminate?}.",
                        "format": {"type": "text"},
                    },
                ],
            },
            *([{"type": "message", "role": "developer", "content": instructions}] if instructions else []),
            *(
                [
                    {
                        "type": "message",
                        "role": "developer",
                        "content": "Active execution handles: " + json.dumps(execution_state, sort_keys=True),
                    }
                ]
                if execution_state and (execution_state["processes"] or execution_state["cells"])
                else []
            ),
            *items,
        ],
        "stream": True,
        "store": False,
        "include": ["reasoning.encrypted_content"],
        "prompt_cache_key": conversation_id,
        "client_metadata": {"session_id": conversation_id, "thread_id": conversation_id, "turn_id": conversation_id},
    }


@dataclass(frozen=True)
class ProviderResponse:
    terminal: dict[str, Any]
    output: list[dict[str, Any]]
    events: list[dict[str, Any]]

    def audit(self) -> dict[str, Any]:
        return {"response": self.terminal, "events": self.events}


@dataclass(frozen=True)
class ResponseEvidence:
    store: Store
    owner: Owner
    intent_id: UUID


async def sample(
    http: ClientSession,
    endpoint: str,
    credential: str,
    body: dict[str, Any],
    *,
    compaction: bool = False,
    evidence: ResponseEvidence | None = None,
    attribution: dict[str, str] | None = None,
) -> ProviderResponse:
    """Require the provider terminal event; never continue from partially streamed items."""
    headers = {"Authorization": f"Bearer {credential}", "x-openai-internal-codex-responses-lite": "true"}
    if attribution is not None:
        headers.update(attribution)
    if compaction:
        headers["x-codex-beta-features"] = "remote_compaction_v2"
    async with http.post(endpoint, json=body, headers=headers) as response:
        if response.status >= 400:
            raise KnownFailureError({"http_status": response.status, "body": await response.text()})
        data: list[str] = []
        events: list[dict[str, Any]] = []
        output: list[dict[str, Any]] = []
        async for line_bytes in response.content:
            line = line_bytes.decode().rstrip("\r\n")
            if line.startswith("data:"):
                data.append(line[5:].lstrip())
            elif not line and data:
                event = json.loads("\n".join(data))
                data.clear()
                if evidence is not None:
                    await evidence.store.provider_event(evidence.owner, evidence.intent_id, event)
                events.append(event)
                if event["type"] == "response.output_item.done":
                    output.append(event["item"])
                if event["type"] == "response.completed":
                    terminal: dict[str, Any] = event["response"]
                    if terminal.get("status", "completed") != "completed":
                        raise KnownFailureError(
                            {"reason": "unexpected terminal status", "response": terminal, "events": events}
                        )
                    if "output" in terminal:
                        if output and output != terminal["output"]:
                            raise KnownFailureError(
                                {"reason": "terminal output disagrees with streamed completed items", "events": events}
                            )
                        output = terminal["output"]
                    return ProviderResponse(terminal, output, events)
                if event["type"] in {"response.failed", "response.incomplete", "error"}:
                    raise KnownFailureError({"terminal": event, "events": events})
    raise ConnectionError("provider stream ended without response.completed; sampling outcome unknown")
