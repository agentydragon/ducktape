local I = import '../../specimens/lib.libsonnet';

// iss-024: Delete unreachable AttributeError fallback - content blocks are always Pydantic models

I.issueOneOccurrence(
  rationale=|||
    The AttributeError fallback in `_render_tool_result` can never be triggered and should be deleted.

    **Current code (lines 85-89):**
    ```python
    elif result.content:
        try:
            data = [block.model_dump(by_alias=True) for block in result.content]
        except AttributeError:
            # Fallback: leave content blocks as-is if not Pydantic models
            data = result.content
    ```

    **Why this fallback is unreachable:**

    1. **Type flow:**
       - `output: ToolCallOutput` has `result: CallToolResult` (handler.py:41-43)
       - `CallToolResult` is from `fastmcp.client.client` (handler.py:14)
       - FastMCP's `CallToolResult.content` contains MCP content types

    2. **Content types are Pydantic models:**
       - `result.content` is a list of MCP content blocks (TextContent, ImageContent, etc.)
       - All MCP content types are Pydantic BaseModels (from `mcp.types`)
       - Evidence from tests: `mcp_types.TextContent(type="text", text="success")` (test_middleware_lifecycle.py:78)
       - Evidence from production: `isinstance(block, mcp_types.TextContent)` (agent.py:124)

    3. **All Pydantic models have model_dump():**
       - `model_dump()` is a core method on all Pydantic BaseModels
       - It cannot raise AttributeError unless the object is not a Pydantic model
       - Since content blocks are guaranteed to be Pydantic models, AttributeError cannot occur

    4. **Type validation ensures correctness:**
       - FastMCP validates content through Pydantic: `TypeAdapter(mcp_types.CallToolResult).validate_python(payload)` (calltool.py:51)
       - If content blocks weren't proper MCP types, validation would fail earlier
       - By the time we reach event_renderer, content is guaranteed valid

    **The only way AttributeError could occur:**
    - Someone manually constructs CallToolResult with non-Pydantic objects in content
    - This would violate type annotations and fail validation
    - This is a programmer error that should fail loudly, not be silently handled

    **Correct code:**
    ```python
    elif result.content:
        data = [block.model_dump(by_alias=True) for block in result.content]
    ```

    **Benefits of deletion:**
    - Removes dead code
    - Removes misleading comment suggesting content might not be Pydantic models
    - Clearer code without defensive fallback for impossible case
    - If someone violates types, they'll get a clear error instead of silent fallback
  |||,
  properties=['dead-code', 'fail-fast', 'type-safety'],
  filesToRanges={
    'adgn/src/adgn/agent/event_renderer.py': [
      [85, 89],  // try/except AttributeError fallback - unreachable
    ],
  },
)
