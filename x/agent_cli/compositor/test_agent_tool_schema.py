"""Agent tool-schema evolution against the agent CLI compositor."""

from __future__ import annotations

import json
from typing import Annotated, Any, Final, Literal, cast

import pytest
import pytest_bazel
from fastmcp import FastMCP
from pydantic import BaseModel, ConfigDict, Field

from agent_core.agent import Agent
from agent_core.handler import FinishOnTextMessageHandler
from agent_core.loop_control import RequireAnyTool
from agent_core.mcp_provider import MCPToolProvider
from agent_core.testing.mcp.responses import MCPDecoratorMock
from openai_utils.model import SystemMessage
from x.agent_cli.compositor.prefix import MCPMountPrefix

# --- Native tool schema tests ---

TOOL_A_NAME: Final[str] = "tool_a"


class ToolAInput(BaseModel):
    """Input for tool A."""

    model_config = ConfigDict(extra="forbid")

    param_x: float = Field(description="First parameter")
    param_y: float = Field(description="Second parameter")


class ToolAResult(BaseModel):
    """Result of tool A."""

    value: float = Field(description="Computed result value")


@pytest.fixture
def mcp_a() -> FastMCP:
    """Create MCP server A with a native function-signature tool."""
    mcp = FastMCP()

    @mcp.tool(output_schema=None)
    def tool_a(
        param_x: Annotated[float, Field(description="First parameter")],
        param_y: Annotated[float, Field(description="Second parameter")],
    ) -> ToolAResult:
        """Perform tool A on the inputs."""
        return ToolAResult(value=param_x + param_y)

    return mcp


class NestedInfo(BaseModel):
    """Nested information block."""

    model_config = ConfigDict(extra="forbid")

    regex: Annotated[str, Field(description="Regex validation", pattern=r"^\d{5}$")]
    text: str = Field(description="Required text")


class CategoryInfo(BaseModel):
    """Category classification."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["type_a", "type_b", "type_c"]


@pytest.fixture
def mcp_b() -> FastMCP:
    """Create MCP server B with a nested, fully required input schema."""
    mcp = FastMCP()

    @mcp.tool(output_schema=None)
    def tool_b(
        identifier: Annotated[str, Field(description="Required regex field", pattern=r"^[a-z]{3}$")],
        count: Annotated[int, Field(description="Int with range", ge=10, le=100)],
        nested: NestedInfo,
        category: CategoryInfo,
        flag: bool,
    ) -> str:
        """Perform tool B with complex validation."""
        return "test"

    return mcp


async def test_agent_compositor_native_tool_request_schema(compositor, compositor_client, mcp_a, mcp_b) -> None:
    """Test agent with native FastMCP servers attached one by one, showing schema evolution.

    This test demonstrates:
    1. Mounting mcp_a alone -> schema has only tool_a
    2. Mounting both servers -> schema has both tool_a and tool_b
    3. Complex Pydantic input schema with:
       - Annotated fields with regex patterns
       - Field descriptions
       - Model-level descriptions
       - Nested models (NestedInfo, CategoryInfo)
    4. Full request structure with all tools/schemas visible to the LLM
    """
    # Mount only mcp_a for phase 1.
    mcp_a_prefix = MCPMountPrefix("mcp_a")
    await compositor.mount_inproc(mcp_a_prefix, mcp_a)
    mcp_tool_provider = MCPToolProvider(compositor_client)

    @MCPDecoratorMock.mock()
    def mock(m: MCPDecoratorMock):
        # Phase 1: only mcp_a mounted
        req = yield
        assert req.tools is not None
        assert {"mcp_a_tool_a"} <= {t.name for t in req.tools}
        print("\nPHASE 1 REQUEST (mcp_a only):")
        print(json.dumps(req.model_dump(exclude_none=True), indent=2))

        yield m.mcp_tool_call(mcp_a_prefix, TOOL_A_NAME, ToolAInput(param_x=10, param_y=20))
        # Phase 1 ends with assistant text; capture Phase 2's initial request
        req = yield m.assistant_text("The result is 30.")

        # Phase 2: both servers mounted (req already contains the request)
        assert req.tools is not None
        assert {"mcp_a_tool_a", "mcp_b_tool_b"} <= {t.name for t in req.tools}
        print("\nPHASE 2 REQUEST (mcp_a + mcp_b):")
        print(json.dumps(req.model_dump(exclude_none=True), indent=2))

        # Detailed schema assertions
        tool_a = next(t for t in req.tools if t.name == "mcp_a_tool_a")
        assert tool_a.description
        assert tool_a.type == "function"
        assert tool_a.parameters["type"] == "object"
        assert tool_a.parameters["properties"]["param_x"]["type"] == "number"
        assert tool_a.parameters["properties"]["param_y"]["type"] == "number"
        assert set(tool_a.parameters["required"]) == {"param_x", "param_y"}

        tool_b = next(t for t in req.tools if t.name == "mcp_b_tool_b")
        assert tool_b.description
        assert tool_b.type == "function"
        params_b = tool_b.parameters
        assert params_b["type"] == "object"

        # Top-level fields
        props = params_b["properties"]
        assert props["identifier"]["type"] == "string"
        assert props["count"]["type"] == "integer"
        assert props["nested"]["type"] == "object"
        assert props["category"]["type"] == "object"
        assert props["flag"]["type"] == "boolean"
        assert set(params_b["required"]) == {"identifier", "count", "nested", "category", "flag"}

        def resolve_definition(schema: dict[str, Any]) -> dict[str, Any]:
            reference = schema.get("$ref")
            if reference is None:
                return schema
            definition_name = reference.rsplit("/", 1)[-1]
            return cast(dict[str, Any], params_b["$defs"][definition_name])

        nested_def = resolve_definition(props["nested"])
        assert nested_def["properties"]["regex"]["pattern"] == r"^\d{5}$"
        assert set(nested_def["required"]) == {"regex", "text"}

        category_def = resolve_definition(props["category"])
        assert set(category_def["properties"]["type"]["enum"]) == {"type_a", "type_b", "type_c"}
        assert category_def["required"] == ["type"]

        print("\nMCP_A_TOOL_A SCHEMA:")
        print(json.dumps(tool_a.model_dump(exclude_none=True), indent=2))

        print("\nMCP_B_TOOL_B SCHEMA:")
        print(json.dumps(tool_b.model_dump(exclude_none=True), indent=2))

        yield m.mcp_tool_call(mcp_a_prefix, TOOL_A_NAME, ToolAInput(param_x=10, param_y=20))
        yield m.assistant_text("The result is 30.")

    system_prompt = "You are a helpful assistant. Calculate 10 + 20."

    print("PHASE 1: MCP_A ONLY")

    agent = await Agent.create(
        tool_provider=mcp_tool_provider,
        client=mock,
        handlers=[FinishOnTextMessageHandler()],
        parallel_tool_calls=False,
        tool_policy=RequireAnyTool(),
    )
    agent.process_message(SystemMessage.text(system_prompt))
    await agent.run()

    print("PHASE 2: MCP_A + MCP_B")

    # Mount mcp_b for phase 2 (mcp_a already mounted)
    await compositor.mount_inproc(MCPMountPrefix("mcp_b"), mcp_b)

    agent = await Agent.create(
        tool_provider=mcp_tool_provider,
        client=mock,
        handlers=[FinishOnTextMessageHandler()],
        parallel_tool_calls=False,
        tool_policy=RequireAnyTool(),
    )
    agent.process_message(SystemMessage.text(system_prompt))
    await agent.run()


if __name__ == "__main__":
    pytest_bazel.main()
