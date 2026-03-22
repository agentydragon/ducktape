# FastMCP + Pydantic: Typed Tool I/O

## Server Pattern

Subclass `EnhancedFastMCP`, expose tools as typed `FunctionTool` attributes:

```python
from fastmcp.tools import FunctionTool
from pydantic import BaseModel
from mcp_infra.enhanced import EnhancedFastMCP
from openai_utils.pydantic_strict_mode import OpenAIStrictModeBaseModel

class MyInput(OpenAIStrictModeBaseModel):
    """Input for my_tool."""
    name: str
    count: int | None = None

class MyOutput(BaseModel):
    result: str

class MyServer(EnhancedFastMCP):
    my_tool: FunctionTool

    def __init__(self):
        super().__init__("My Server", instructions="...")

        def my_tool(input: MyInput) -> MyOutput:
            """Tool description for the LLM."""
            return MyOutput(result=f"Processed {input.name}")

        self.my_tool = self.flat_model()(my_tool)
```

Tool names derive from function names. Access via `server.my_tool.name`.

## Input Models

**Always use `OpenAIStrictModeBaseModel`** (auto-validates at class definition):

- `T | None = None` for optional fields (all fields must be in `required` array)
- `list[T]` not `set[T]` (uniqueItems not allowed in strict mode)
- `str` not `Path` (format="path" not allowed)
- `Field(description=...)` for LLM-facing docs
- No `dict[str, Any]`, `Any`, `*args`, `**kwargs`, untyped params, or dataclasses

## Output Models

- Structured data: Pydantic `BaseModel` (no strict mode requirement)
- Simple value: primitive (`str`, `int`)
- Acknowledgment: actionable string (`f"Item {id} deleted"`)

## TypeAdapter

Only for parsing outside tools (tests, ad-hoc validation). Inside tools, FastMCP validates inputs automatically.

## Schema Wiring

MCP `list_tools` -> `inputSchema` (JSON Schema) -> mapped to OpenAI/Anthropic tool definitions as `{server}_{tool}`. Correct types = correct schema = no need to restate parameters in prompts.
