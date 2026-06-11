# FastMCP tool schemas (Pydantic)

## Why this matters

- LLMs plan tool calls from the advertised JSON Schema; correct schemas make calls reliable and remove the need to restate parameter shapes in prompt prose.
- Our adapters surface FastMCP tool schemas directly to the model as OpenAI/Anthropic tool definitions.

## Canonical server pattern (use this by default)

**Server implementation:** Subclass `EnhancedFastMCP` and expose tools as typed attributes:

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
    """Output can be any Pydantic model (no strict mode requirement)."""
    result: str
    items: list[str]

class MyServer(EnhancedFastMCP):
    """My MCP server with typed tool access."""

    # Tool attributes (assigned in __init__)
    my_tool: FunctionTool

    def __init__(self, config: MyConfig):
        super().__init__("My Server", instructions="Server instructions...")

        # Register tool - name derived from function name
        def my_tool(input: MyInput) -> MyOutput:
            """Tool description for the LLM."""
            return MyOutput(result=f"Processed {input.name}", items=[])

        self.my_tool = self.flat_model()(my_tool)
```

**Key principles:**

- **Input models:** Use `OpenAIStrictModeBaseModel` (validates at class definition time)
- **Output models:** Use regular Pydantic `BaseModel` (no strict mode requirement)
- **Tool names:** Derived from function names (no explicit `name=` parameter)
- **Tool attributes:** Typed as `FunctionTool` for programmatic access elsewhere
- **Single source of truth:** Access tool name via `server.my_tool.name` (no string literals)

## Input Models (OpenAI Strict Mode)

**Default:** All MCP tool inputs should use `OpenAIStrictModeBaseModel`

```python
from openai_utils.pydantic_strict_mode import OpenAIStrictModeBaseModel

class MyToolInput(OpenAIStrictModeBaseModel):
    # str not Path (format="path" not allowed)
    cwd: str | None = None
    # list not set (uniqueItems not allowed)
    files: list[str]
    max_bytes: int = Field(ge=0, le=100_000, description="Maximum bytes to read")
```

**Validation:** Automatic at class definition time - raises `OpenAIStrictModeValidationError` if incompatible

**Key rules:**

- Use `T | None = None` for optional fields (all fields must be in `required` array)
- Use `list[T]` not `set[T]` (uniqueItems not allowed)
- Use `str` not `Path` (format="path" not allowed)
- Use `Field(description=...)` for LLM-facing documentation
- Keep models small and focused; nest when needed

## Output Models (No Strict Mode Requirement)

**Structured outputs:** Return Pydantic models when tool produces structured data

```python
class ToolResult(BaseModel):
    """Tool results can use regular BaseModel."""
    status: Literal["success", "failure"]
    message: str
    data: dict[str, Any] | None = None
```

**Simple outputs:** Return primitives when that's the natural result

```python
def create_item(input: CreateInput) -> str:
    """Returns the created item's ID."""
    item_id = db.create(input.name)
    return item_id  # Just return the ID string
```

**Acknowledgments:** Return actionable strings for operations with no semantic output

```python
def delete_item(input: DeleteInput) -> str:
    """Returns confirmation message."""
    db.delete(input.item_id)
    return f"Item {input.item_id} successfully deleted"
```

## DON'Ts (these degrade/break schema)

- Don't use `dict[str, Any]`, `Any`, `*args`, `**kwargs`, or untyped params for inputs
- Don't use dataclasses as input (use Pydantic models)
- Don't use regular `BaseModel` for inputs (use `OpenAIStrictModeBaseModel`)
- Don't restate the schema in prose; rely on the JSON Schema

- Programmatic: connect directly to your FastMCP server or a Compositor client and list tools

```python
# Quick check snippet (in-proc server)
import asyncio
from fastmcp.client import Client
from my_server import make_server  # returns a FastMCP("demo") instance

async def main():
    server = make_server()
    async with Client(server) as client:
        tools = await client.list_tools()
        for t in tools:
            if t.name == "done":
                # JSON Schema for the input model
                print(t.inputSchema)
asyncio.run(main())
```

- Manual: log/print the schema FastMCP emits (client.list_tools()), confirm:
  - type: "object"
  - properties include your fields (e.g., outcome, summary)
  - required matches your model (e.g., summary optional if default)
  - unions show as oneOf with discriminator

## Our wiring (FastMCP → OpenAI/Claude)

- We map MCP list_tools into OpenAI/Anthropic tool definitions:
  - name: `<server>_<tool>`
  - description: FastMCP tool description
  - parameters: inputSchema as returned by FastMCP (JSON Schema)
- If your tool is correctly typed, the model sees the exact parameter schema and can call it without extra prompt instructions.

## Common pitfalls and fixes

- Using regular `BaseModel` for inputs → use `OpenAIStrictModeBaseModel` instead
- Multiple positional params → consolidate into a single input model
- Using `Path`, `set[T]`, or other strict-mode-incompatible types → use `str`, `list[T]`
- Using `Any` or `dict[str, Any]` for inputs → replace with concrete types
- Over-engineering outputs → return primitives when appropriate (e.g., just the ID string)

# FastMCP + Pydantic: Typed tool I/O (canonical patterns)

## Scope

- How to define FastMCP tools with precise, validated inputs and stable, typed outputs
- When to use Pydantic TypeAdapter explicitly (rare)

## Core rules

- **Inputs:** Use `OpenAIStrictModeBaseModel` for all tool inputs
  - FastMCP parses and validates the inbound dict against your model automatically
  - Auto-validates at class definition time
- **Outputs:** Choose based on semantic meaning:
  - Structured data → Pydantic `BaseModel`
  - Simple value → primitive (str, int, bool)
  - Acknowledgment → actionable string message
- **Don't hand-parse:** FastMCP already validates inputs
  - Manual TypeAdapter is only for ad-hoc parsing outside FastMCP's auto-path (e.g., tests)

## Complete example (server with tool)

```python
from fastmcp.tools import FunctionTool
from pydantic import BaseModel
from mcp_infra.enhanced import EnhancedFastMCP
from openai_utils.pydantic_strict_mode import OpenAIStrictModeBaseModel

# Input: OpenAI strict mode required
class ProcessInput(OpenAIStrictModeBaseModel):
    """Input for process_data tool."""
    file_path: str
    max_items: int | None = None

# Output: regular BaseModel (no strict mode requirement)
class ProcessResult(BaseModel):
    """Result of processing."""
    items_processed: int
    warnings: list[str]

class MyServer(EnhancedFastMCP):
    """My MCP server."""

    process_data_tool: FunctionTool

    def __init__(self):
        super().__init__("My Server", instructions="Process data...")

        def process_data(input: ProcessInput) -> ProcessResult:
            """Process data from file."""
            # input is already validated (no TypeAdapter needed)
            count = do_processing(input.file_path, input.max_items)
            return ProcessResult(items_processed=count, warnings=[])

        self.process_data_tool = self.flat_model()(process_data)
```

Testing and ad‑hoc parsing (TypeAdapter)

- Use TypeAdapter only when you need to parse a dict/JSON outside a tool (e.g., tests asserting server output):

```python
from pydantic import TypeAdapter
page = TypeAdapter(TextPage).validate_python(payload_dict)
# or
page = TypeAdapter(TextPage).validate_json(payload_json)
```

- Inside tools: do not call TypeAdapter; FastMCP validates parameters before your function runs, and serializes BaseModel returns to structuredContent.

## Best practices

- **Input models:** Always use `OpenAIStrictModeBaseModel` (validates at class definition)
- **Output choice:**
  - Structured → Pydantic model
  - ID/token/simple value → primitive
  - Acknowledgment → string like "File successfully uploaded"
- **Descriptions:** Use `Field(description=...)` to help the LLM planner
- **Keep focused:** Small models with clear purpose; nest when needed
- **Server pattern:** Subclass `EnhancedFastMCP`, expose tools as typed attributes

## When to use TypeAdapter

- Converting free-form JSON (not bound to a tool) into a typed model
- Validating nested fragments from external APIs
- Tests asserting on structured payloads
- **Not in tool bodies:** FastMCP validates inputs automatically

Version notes

- Structured outputs and output schemas are supported in FastMCP ≥ 2.10
- FastMCP preserves Annotated metadata (include_extras=True), so discriminators are exported in JSON Schema

References

- FastMCP tools (Pydantic models, structured output, output schemas): <https://gofastmcp.com/servers/tools>
- Pydantic v2 TypeAdapter: <https://docs.pydantic.dev/latest/usage/validators/#typeadapter>
