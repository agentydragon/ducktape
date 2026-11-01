"""Manual live smoke of Ollama routes offered by the deployed model catalog."""

from __future__ import annotations

import asyncio
import json
import os

import pytest_bazel

from agentplane.app.testing.client import Client
from agentplane.env.runtime.harnesshost.harness import Harness
from agentplane.proto import event_pb2, session_pb2
from agentplane.runner_gateway.dispatcher.testing_client import SessionDispatcherClient
from agentplane.sandboxes.runtimeconfig.model_catalog import ModelCatalog
from agentplane.system_tests.agent import Agent
from agentplane.system_tests.ollama_catalog import ollama_cases
from agentplane.system_tests.sandbox import Sandboxes
from util.testing.undeclared_outputs import undeclared_outputs_dir

# gazelle:include_dep @pypi//protobuf


def _id(case: tuple[session_pb2.Harness, str]) -> str:
    harness, route = case
    return f"{session_pb2.Harness.Name(harness).lower()}-{route.replace('/', '-')}"


async def _exercise_case(
    client: Client,
    dispatcher: SessionDispatcherClient,
    sandbox: Sandboxes,
    catalog: ModelCatalog,
    harness: session_pb2.Harness,
    route: str,
) -> None:
    case_id = _id((harness, route))
    evidence: dict[str, object] = {"case": case_id, "route": route, "status": "started"}
    try:
        offered = catalog.harnesses[Harness(session_pb2.Harness.Name(harness))]
        assert route in offered, f"route absent from offered models for {case_id}"
        view = await sandbox("systest-ollama-smoke")
        evidence["sandbox"] = view.name
        agent = await Agent.open(
            client, dispatcher, sandbox=view.name, sandbox_uid=view.uid, harness=harness, model=route
        )
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
        codex_windows = [
            frame["params"]["tokenUsage"]["modelContextWindow"]
            for frame in native
            if frame.get("method") == "thread/tokenUsage/updated"
        ]
        claude_windows = [
            usage["contextWindow"]
            for frame in native
            if frame.get("type") == "result"
            for usage in frame.get("modelUsage", {}).values()
        ]
        evidence["context_windows"] = sorted(
            {value for value in (*codex_windows, *claude_windows) if isinstance(value, int)}
        )
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


async def test_ollama_tool_call(client: Client, dispatcher: SessionDispatcherClient, sandbox: Sandboxes) -> None:
    catalog = await client.models()
    cases = ollama_cases(catalog)
    selected = os.environ.get("OLLAMA_SMOKE_CASE")
    if selected:
        cases = [case for case in cases if _id(case) == selected]
        if not cases:
            raise ValueError(f"unknown OLLAMA_SMOKE_CASE: {selected}")
    for harness, route in cases:
        await _exercise_case(client, dispatcher, sandbox, catalog, harness, route)


if __name__ == "__main__":
    pytest_bazel.main()
