"""Tests for MCPResponsesFactory convenience methods."""

from __future__ import annotations

import pytest_bazel
from hamcrest import all_of, assert_that, has_properties, instance_of
from pydantic import BaseModel

from agent_core.testing.matchers import has_json_arguments
from agent_core.testing.mcp.responses import MCPResponsesFactory
from mcp_infra.prefix import MCPMountPrefix
from openai_utils.model import FunctionCallItem

SERVER = MCPMountPrefix("test_server")
TOOL = "do_thing"


class ThingInput(BaseModel):
    text: str
    count: int = 1


def test_mcp_tool_call_explicit_id(responses_factory: MCPResponsesFactory):
    call = responses_factory.mcp_tool_call(SERVER, TOOL, ThingInput(text="hello", count=2), call_id="call_1")

    assert_that(
        call,
        all_of(
            instance_of(FunctionCallItem),
            has_properties(name="test_server_do_thing", call_id="call_1"),
            has_json_arguments({"text": "hello", "count": 2}),
        ),
    )


if __name__ == "__main__":
    pytest_bazel.main()
