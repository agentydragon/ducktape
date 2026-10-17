from __future__ import annotations

import pytest_bazel
from pydantic import BaseModel

from mcp_infra.enhanced.server import EnhancedFastMCP
from mcp_infra.prefix import MCPMountPrefix
from mcp_infra.tool_schemas import extract_tool_input_schemas, extract_tool_schemas


class SumOutput(BaseModel):
    value: int


def test_native_function_signature_models_for_display() -> None:
    server = EnhancedFastMCP("schema-tools")

    @server.tool(output_schema=None)
    def add(left: int, right: int = 0) -> SumOutput:
        return SumOutput(value=left + right)

    prefix = MCPMountPrefix("math")
    input_schemas = extract_tool_input_schemas({prefix: server})
    output_schemas = extract_tool_schemas({prefix: server})

    input_model = input_schemas[(prefix, "add")]
    assert input_model.model_validate({"left": 3}).model_dump() == {"left": 3, "right": 0}
    assert output_schemas[(prefix, "add")] is SumOutput


if __name__ == "__main__":
    pytest_bazel.main()
