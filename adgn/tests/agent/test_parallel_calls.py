import asyncio
import json
import time
from typing import Any
from unittest.mock import patch

from fastmcp.server import FastMCP

from adgn.agent.agent import MiniCodex
from adgn.agent.loggers import RecordingHandler
from adgn.agent.loop_control import Abort, Auto, Continue
from adgn.agent.reducer import BaseHandler
from adgn.mcp._shared.naming import build_mcp_function
from adgn.openai_utils.model import FunctionCallItem
from tests.agent.helpers import NoopOpenAIClient


class OneShotSyntheticHandler(BaseHandler):
    """Handler that returns a SyntheticAction once, then Abort."""

    def __init__(self, outputs: list[Any]):
        self._done = False
        self._outputs = outputs

    def on_before_sample(self):  # returns SyntheticAction first, then Abort
        if not self._done:
            self._done = True
            return Continue(Auto(), inserts_input=tuple(self._outputs), skip_sampling=True)
        return Abort()

    # No-ops for hooks used by agent
    def on_reasoning(self, *_a, **_k):  # pragma: no cover - not used here
        return None

    def on_assistant_text(self, *_a, **_k):  # pragma: no cover - not used here
        return None

    def on_tool_call(self, *_a, **_k):  # pragma: no cover - not used here
        return None

    def on_tool_result_event(self, *_a, **_k):  # pragma: no cover - not used here
        return None


def _make_slow_server(per_call_secs: float = 0.30) -> FastMCP:
    """Return a FastMCP server implementing two slow tools.

    The tools are async and sleep for per_call_secs to simulate latency. This
    exercises the real inproc FastMCP transport in tests (higher fidelity).
    """
    mcp = FastMCP("dummy")

    @mcp.tool()
    async def slow() -> dict[str, Any]:
        await asyncio.sleep(per_call_secs)
        return {"ok": True, "tool": "slow", "args": {}}

    @mcp.tool()
    async def slow2() -> dict[str, Any]:
        await asyncio.sleep(per_call_secs)
        return {"ok": True, "tool": "slow2", "args": {}}

    return mcp


async def test_parallel_tool_calls_reduce_wall_time(make_compositor):
    # Build a real inproc FastMCP server with two slow tools

    # Two tool calls with ~0.30s latency each; if run in parallel, wall time ~0.30-0.45s
    tc1 = FunctionCallItem(name=build_mcp_function("dummy", "slow"), call_id="call_1", arguments=json.dumps({}))
    tc2 = FunctionCallItem(name=build_mcp_function("dummy", "slow2"), call_id="call_2", arguments=json.dumps({}))

    handler = OneShotSyntheticHandler(outputs=[tc1, tc2])

    rec = RecordingHandler()

    async with make_compositor({"dummy": _make_slow_server()}) as (mcp_client, _comp):
        agent = await MiniCodex.create(
            model="noop",
            system="test",
            mcp_client=mcp_client,
            client=NoopOpenAIClient(),  # SyntheticAction path bypasses OpenAI
            parallel_tool_calls=True,
            handlers=[handler, rec],
        )

        t0 = time.perf_counter()
        await agent.run("go")

        # Wait for recording handler to observe expected events (tool_call + function_call_output)
        # Set up event-driven completion notification
        completion_event = asyncio.Event()
        target_records = 4  # 2 tools x 2 events each

        # Store original methods
        original_on_tool_call = rec.on_tool_call_event
        original_on_tool_result = rec.on_tool_result_event

        def check_and_signal():
            if len(rec.records) >= target_records:
                completion_event.set()

        def enhanced_tool_call(evt):
            original_on_tool_call(evt)  # Call original method
            check_and_signal()

        def enhanced_tool_result(evt):
            original_on_tool_result(evt)  # Call original method
            check_and_signal()

        # Use proper mocking to patch the methods
        with (
            patch.object(rec, "on_tool_call_event", side_effect=enhanced_tool_call),
            patch.object(rec, "on_tool_result_event", side_effect=enhanced_tool_result),
        ):
            await asyncio.wait_for(completion_event.wait(), timeout=2.0)
        elapsed = time.perf_counter() - t0

    # Assert shorter than serial (~0.60s), with generous headroom for CI noise
    # Threshold tuned for CI noise; serial takes ~0.60s, expect faster here
    assert elapsed < 0.55, f"expected parallel speedup; took {elapsed:.3f}s"

    # Sanity checks on outputs/metrics via recording handler
    tc_count = len([e for e in rec.records if e.get("kind") == "tool_call"])
    function_call_output_count = len([e for e in rec.records if e.get("kind") == "function_call_output"])
    assert tc_count >= 2
    assert function_call_output_count >= 2
    kinds = [evt.get("kind") for evt in rec.records if isinstance(evt, dict)]
    assert kinds.count("tool_call") >= 2
    assert kinds.count("function_call_output") >= 2
