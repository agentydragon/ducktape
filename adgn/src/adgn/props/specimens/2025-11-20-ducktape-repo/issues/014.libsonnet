local I = import '../../specimens/lib.libsonnet';

// iss-014: from_server method is too long and complex, extract loop body

I.issueOneOccurrence(
  rationale=|||
    The `TypedClient.from_server` classmethod is 69 lines long (lines 109-178), with a
    single for-loop body spanning 49 lines (lines 128-177). This makes the method difficult
    to understand and maintain.

    **Current structure:**
    - Lines 109-127: Setup and error handling
    - Lines 128-177: Giant for-loop that introspects each tool
    - Line 178: Return statement

    **Why this is problematic:**
    - Single method doing too many things: registry access, tool introspection, type
      resolution, model extraction
    - 49-line loop body is extremely hard to read and reason about
    - Multiple nested try/except blocks and conditionals within the loop
    - Mixing different concerns: FastMCP API access, attribute extraction, type checking,
      model resolution
    - Hard to test individual pieces of the introspection logic

    **Recommended refactor:**
    Extract the loop body into a helper method like:
    ```python
    @staticmethod
    def _extract_tool_models(tool: Any) -> tuple[str, ToolModels] | None:
        """Extract tool name and models from a FastMCP tool object.

        Returns None if tool cannot be introspected.
        """
        # Lines 129-177 moved here
        ...
        return (tool_key, ToolModels(...))
    ```

    Then simplify the main loop to:
    ```python
    for t in tools:
        result = cls._extract_tool_models(t)
        if result is not None:
            tool_key, tool_models = result
            client._models[tool_key] = tool_models
    ```

    **Benefits:**
    - Each method has a single responsibility
    - Easier to understand the flow in from_server
    - Helper method can be tested independently
    - Reduces cognitive load when reading either method
    - Makes it clear that the loop is "extract and store" pattern
  |||,
  properties=['code-complexity', 'readability', 'maintainability', 'single-responsibility'],
  filesToRanges={
    'adgn/src/adgn/mcp/stubs/typed_stubs.py': [
      [109, 178],  // Entire from_server method
      [128, 177],  // Giant for-loop body that should be extracted
    ],
  },
)
