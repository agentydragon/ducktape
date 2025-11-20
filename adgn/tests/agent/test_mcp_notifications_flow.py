from __future__ import annotations

from typing import Any

from fastmcp.server import FastMCP
from fastmcp.server.context import Context
from hamcrest import (
    all_of,
    assert_that,
    contains_string,
    greater_than_or_equal_to,
    has_item,
    has_length,
    has_properties,
    instance_of,
    matches,
)
from pydantic import BaseModel, ConfigDict
import pytest

from adgn.agent.agent import MiniCodex
from adgn.agent.reducer import AutoHandler, NotificationsHandler
from adgn.mcp._shared.fastmcp_flat import mcp_flat_model
from adgn.mcp._shared.naming import build_mcp_function
from adgn.mcp.notifying_fastmcp import NotifyingFastMCP
from adgn.mcp.stubs.typed_stubs import ToolStub
from adgn.openai_utils.model import InputTextPart, ResponsesRequest, ResponsesResult, UserMessage
from tests.fixtures.responses import ResponsesFactory
from tests.llm.support.openai_mock import make_mock


class NotifyPolicyInput(BaseModel):
    uri: str = "notifier://policy.py"
    model_config = ConfigDict(extra="forbid")


class NotifyPolicyOutput(BaseModel):
    ok: bool
    uri: str
    model_config = ConfigDict(extra="forbid")


class _NotifierServer(FastMCP):
    """Test helper FastMCP that can emit resource-updated notifications via a callback.

    Emits ResourceUpdated notifications via the protocol inside tool logic.
    """

    def __init__(self) -> None:
        super().__init__(name="notifier", instructions="Test notifier server")

        @mcp_flat_model(
            self,
            name="notify_policy",
            title="Notify policy",
            description="Emit a ResourceUpdated notification",
            structured_output=True,
        )
        async def notify_policy(input: NotifyPolicyInput, ctx: Context) -> NotifyPolicyOutput:
            # Protocol-level notification: emit ResourceUpdatedNotification from server to client
            sess = ctx.request_context.session  # low-level ServerSession
            await sess.send_resource_updated(input.uri)
            return NotifyPolicyOutput(ok=True, uri=input.uri)


@pytest.fixture
def server() -> FastMCP:
    return _NotifierServer()


async def test_notifications_pre_sampling_out_of_band(
    server: FastMCP, responses_factory: ResponsesFactory, make_buffered_client
) -> None:
    # Buffered client path via shared fixture
    async with make_buffered_client({"notifier": server}) as (mcp_client, _comp, buf):
        # Prime a protocol-level notification before sampling by calling the server tool once
        stub = ToolStub(mcp_client, build_mcp_function("notifier", "notify_policy"), NotifyPolicyOutput)
        await stub(NotifyPolicyInput())

        captured: list[ResponsesRequest] = []

        async def _create(req: ResponsesRequest) -> ResponsesResult:
            captured.append(req)
            result: ResponsesResult = responses_factory.make_assistant_message("ok")
            return result

        client = make_mock(_create)
        agent = await MiniCodex.create(
            model="test-model",
            mcp_client=mcp_client,
            handlers=[NotificationsHandler(buf.poll), AutoHandler()],
            client=client,
            system="n/a",
        )
        await agent.run("hello")

        # Inspect the input passed to Responses.create; expect a system notification insert
        assert captured, "expected at least one responses.create call"

        def _has_sysfyi(req: ResponsesRequest) -> bool:
            inp = req.input or []
            for msg in inp:
                if isinstance(msg, UserMessage):
                    for c in msg.content or []:
                        if isinstance(c, InputTextPart) and "<system notification>" in c.text:
                            payload = c.text.split("\n", 1)[-1]
                            if "policy.py" in payload:
                                return True
            return False

        assert_that(captured, has_item(matches(_has_sysfyi)), "expected system notification in request input")


async def test_notifications_within_turn_from_tool(
    server: FastMCP, responses_factory: ResponsesFactory, make_buffered_client
):
    # Build notifier server and manager
    stage = {"n": 0}
    captured: list[ResponsesRequest] = []

    async def _create(req: ResponsesRequest) -> ResponsesResult:
        captured.append(req)
        stage["n"] += 1
        if stage["n"] == 1:
            # First model output: ask to call notifier.notify_policy
            tool_call_result: ResponsesResult = responses_factory.make_tool_call(
                build_mcp_function("notifier", "notify_policy"), {}
            )
            return tool_call_result
        # Second (and later) model output: nothing else to do
        assistant_result: ResponsesResult = responses_factory.make_assistant_message("done")
        return assistant_result

    async with make_buffered_client({"notifier": server}) as (mcp_client, _comp, buf):
        client = make_mock(_create)
        agent = await MiniCodex.create(
            model="test-model",
            mcp_client=mcp_client,
            handlers=[NotificationsHandler(buf.poll), AutoHandler()],
            client=client,
            system="n/a",
        )
        await agent.run("go")

        # The second create call (post-tool) should include the injected system notification
        assert_that(captured, has_length(greater_than_or_equal_to(2)), "expected at least two sampling calls")
        assert_that(
            captured[-1].input,
            has_item(
                all_of(
                    instance_of(UserMessage),
                    has_properties(
                        content=has_item(
                            all_of(instance_of(InputTextPart), has_properties(text=contains_string("<system notification>")))
                        )
                    ),
                )
            ),
            "expected system notification in last sampling call",
        )


async def test_notifications_broadcast_outside_tool(responses_factory: ResponsesFactory, make_buffered_client):
    # Server that can broadcast notifications outside a tool
    server = NotifyingFastMCP(name="notifier", instructions="Notifier test")

    @server.tool()
    async def prime() -> dict[str, Any]:
        return {"ok": True}

    # no server specs needed for this test
    captured: list[ResponsesRequest] = []

    async def _create(req: ResponsesRequest) -> ResponsesResult:
        captured.append(req)
        result: ResponsesResult = responses_factory.make_assistant_message("ok")
        return result

    async with make_buffered_client({"notifier": server}) as (mcp_client, _comp, buf):
        # Establish session (prime) then broadcast outside any tool handler
        await mcp_client.call_tool(name=build_mcp_function("notifier", "prime"), arguments={})
        await server.broadcast_resource_updated("notifier://policy.py")

        client = make_mock(_create)
        agent = await MiniCodex.create(
            model="test-model",
            mcp_client=mcp_client,
            handlers=[NotificationsHandler(buf.poll), AutoHandler()],
            client=client,
            system="n/a",
        )
        await agent.run("hello")

        # Expect notification inserted before sampling
        def _has_sysfyi(req: ResponsesRequest) -> bool:
            for msg in req.input or []:
                if isinstance(msg, UserMessage):
                    for c in msg.content or []:
                        if isinstance(c, InputTextPart) and "<system notification>" in c.text:
                            payload = c.text.split("\n", 1)[-1]
                            if "policy.py" in payload:
                                return True
            return False

        assert_that(captured, has_item(matches(_has_sysfyi)), "expected system notification after out-of-tool broadcast")
