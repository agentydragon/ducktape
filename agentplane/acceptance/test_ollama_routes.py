"""Manual live smoke of every Ollama chat route through both deployed harnesses."""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Awaitable, Callable

import pytest
import pytest_bazel

from agentplane.acceptance.agent import Agent
from agentplane.app.client import Client
from agentplane.app.inventory import SandboxView
from agentplane.app.presets import Harness
from agentplane.protocol import event_pb2
from agentplane.runner import protocol_pb2
from util.testing.undeclared_outputs import undeclared_outputs_dir

# gazelle:include_dep @pypi//protobuf

Sandboxes = Callable[..., Awaitable[SandboxView]]
MODELS = (
    ("gpt-oss-20b", ("128k", "256k", "512k", "1m")),
    ("gpt-oss-120b", ("128k",)),
    ("gemma4-31b-it-q8_0", ("128k",)),
)
VARIANTS = [(model, context) for model, contexts in MODELS for context in contexts]
VARIANTS.sort(key=lambda variant: variant[1] != "128k")
CASES = [
    (harness, f"ollama/{wire}/{model}-{context}")
    for model, context in VARIANTS
    for wire in ("oai-chat", "olm-chat")
    for harness in (protocol_pb2.HARNESS_CLAUDE, protocol_pb2.HARNESS_CODEX)
]


def _id(case: tuple[protocol_pb2.Harness, str]) -> str:
    harness, route = case
    return f"{protocol_pb2.Harness.Name(harness).lower()}-{route.replace('/', '-')}"


selected = os.environ.get("OLLAMA_SMOKE_CASE")
if selected:
    CASES = [case for case in CASES if _id(case) == selected]
    if not CASES:
        raise ValueError(f"unknown OLLAMA_SMOKE_CASE: {selected}")


@pytest.mark.parametrize(("harness", "route"), CASES, ids=[_id(case) for case in CASES])
async def test_ollama_tool_call(client: Client, sandbox: Sandboxes, harness: protocol_pb2.Harness, route: str) -> None:
    case_id = _id((harness, route))
    evidence: dict[str, object] = {"case": case_id, "route": route, "status": "started"}
    try:
        offered = (await client.models())[Harness(protocol_pb2.Harness.Name(harness))]
        assert route in offered, f"route absent from offered models for {case_id}"
        view = await sandbox("accept-ollama-smoke")
        evidence["sandbox"] = view.name
        agent = await Agent.open(client, sandbox=view.name, harness=harness, model=route)
        evidence["thread_id"] = str(agent.thread_id)
        async with asyncio.timeout(300):
            turn = await agent.run(
                "Use your shell tool to run `pwd` and `printf 'OLLAMA_SMOKE_TOOL_OK\\n'`. "
                "Then reply briefly with the working directory you observed."
            )
        evidence["turn_status"] = event_pb2.TurnStatus.Name(turn.status) if turn.status is not None else None
        evidence["tool_outputs"] = [output[:300] for output in turn.tool_outputs]
        evidence["answer"] = turn.answer[:300]
        native = [json.loads(line) for line in turn.native]
        evidence["native_methods"] = sorted(
            {frame["method"] for frame in native if isinstance(frame.get("method"), str)}
        )
        evidence["native_item_types"] = sorted(
            {
                item["type"]
                for frame in native
                if isinstance((params := frame.get("params")), dict)
                if isinstance((item := params.get("item")), dict)
                if isinstance(item.get("type"), str)
            }
        )
        assert turn.tool_outputs, "no completed tool output was recorded"
        assert any("OLLAMA_SMOKE_TOOL_OK" in output for output in turn.tool_outputs), (
            "shell tool did not return the requested marker"
        )
        assert "/state/work" in "\n".join(turn.tool_outputs), "shell tool did not report the expected working directory"
        evidence["status"] = "passed"
    except Exception as error:
        evidence["status"] = "failed"
        evidence["error_type"] = type(error).__name__
        evidence["error"] = str(error)[:500]
        raise
    finally:
        (undeclared_outputs_dir() / f"ollama-smoke-{case_id}.json").write_text(json.dumps(evidence, indent=2))
        hold_seconds = min(max(int(os.environ.get("OLLAMA_SMOKE_HOLD_SECONDS", "0")), 0), 30)
        if hold_seconds:
            await asyncio.sleep(hold_seconds)


if __name__ == "__main__":
    pytest_bazel.main()
