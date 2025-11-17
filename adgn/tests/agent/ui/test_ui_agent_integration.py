from __future__ import annotations

import asyncio
import json

from tests.agent.ui.typed_asserts import assert_typed_items_have_one, is_assistant_markdown
from tests.fixtures.responses import ResponsesFactory
from tests.llm.support.openai_mock import make_mock

from adgn.agent.agent import MiniCodex
from adgn.agent.handler import ContinueDecision
from adgn.agent.notifications.types import NotificationsBatch
from adgn.agent.server.bus import ServerBus
from adgn.agent.server.mode_handler import ServerModeHandler
from adgn.agent.server.runtime import AgentSession, ConnectionManager
from adgn.mcp._shared.naming import build_mcp_function
from adgn.mcp.ui.server import make_ui_server


def _make_ui_behavior(rf: ResponsesFactory):
    calls = {"n": 0}

    async def _behavior(req):
        calls["n"] += 1
        if calls["n"] == 1:
            args_json = json.dumps({"mime": "text/markdown", "content": "**hello**"})
            return rf.make(
                rf.tool_call(
                    call_id="call_1", name=build_mcp_function("ui", "send_message"), arguments=json.loads(args_json)
                )
            )
        return rf.make(rf.tool_call(call_id="call_2", name=build_mcp_function("ui", "end_turn"), arguments={}))

    return _behavior


async def test_ui_server_with_mock_agent_produces_ui_state_updates(
    responses_factory: ResponsesFactory, make_pg_compositor, stub_approval_policy_engine, approval_policy_reader_stub
):
    # Per-agent bus and UI MCP server
    bus = ServerBus()
    ui_server = make_ui_server("ui", bus)
    # no server specs required for this test

    captured: list[dict] = []

    class _NoopPersist:
        async def start_run(self, **kwargs):
            return None

        async def finish_run(self, *args, **kwargs):
            return None

    mgr = ConnectionManager()
    sess = AgentSession(mgr, persistence=_NoopPersist())
    # Wire the per-agent bus so manager drains it on function outputs
    sess.ui_bus = bus
    sess.approval_engine = stub_approval_policy_engine

    # Patch send_json to capture envelopes
    orig_send_json = mgr.send_json

    async def _capture(payload: dict):
        captured.append(payload)
        # Auto-approve tool calls via the server's approval hub when running headless
        try:
            pl = payload.get("payload") if isinstance(payload, dict) else None
            if isinstance(pl, dict) and pl.get("type") == "approval_pending":
                call_id = pl.get("call_id") or ""
                # Defer resolution slightly to avoid resolving within send pipeline
                asyncio.get_running_loop().call_soon(sess.approval_hub.resolve, call_id, ContinueDecision())
        except Exception:
            # Tests should fail loudly elsewhere; keep capture non-fatal
            pass
        await orig_send_json(payload)

    mgr.send_json = _capture  # type: ignore[assignment]

    async with make_pg_compositor({"ui": ui_server, "approval_policy": approval_policy_reader_stub}) as (
        mcp_client,
        _comp,
    ):
        handlers = [ServerModeHandler(bus=bus, poll_notifications=lambda: NotificationsBatch())]
        agent = await MiniCodex.create(
            model="test-model",
            mcp_client=mcp_client,
            handlers=handlers,
            client=make_mock(_make_ui_behavior(responses_factory)),
            system="Use ui tools",
        )
        sess.attach_agent(agent)

        # Kick off a run and wait for it to finish (end_turn triggers Abort)
        await sess.run("hello")
        # Wait for the background run task to complete
        task = sess._task
        if task is not None:
            await task
        # Ensure any queued manager tasks are flushed and bus drained
        await mgr.flush()
        await mgr._emit_ui_bus_messages()

    # Verify directly on session state using typed matcher
    items = sess.ui_state.items
    assert_typed_items_have_one(items, is_assistant_markdown("**hello**"))
