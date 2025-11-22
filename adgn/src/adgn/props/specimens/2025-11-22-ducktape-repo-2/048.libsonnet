local I = import '../../specimens/lib.libsonnet';

// iss-048: Useless docstring on get_tool_call method

I.issueOneOccurrence(
  rationale=|||
    The get_tool_call method has a docstring that provides zero value beyond what's
    already obvious from the method signature:

    ```python
    async def get_tool_call(self, call_id: str) -> ToolCallRecord | None:
        """Get a tool call record by call_id."""
        ...
    ```

    The docstring "Get a tool call record by call_id" merely restates:
    - Method name: get_tool_call → "Get a tool call"
    - Parameter name: call_id → "by call_id"
    - Return type: ToolCallRecord | None → "record"

    This is a textbook example of useless documentation that should be removed.
    Good documentation explains WHY or HOW, not WHAT (which is already clear from
    the signature).

    Fix: Remove the docstring entirely. The method signature is self-documenting.
  |||,
  properties=['no-useless-docs'],
  filesToRanges={
    'adgn/src/adgn/agent/persist/__init__.py': [
      [175, 176],  // Protocol method with useless docstring
    ],
    'adgn/src/adgn/agent/persist/sqlite.py': [
      [466, 467],  // Implementation with useless docstring
    ],
  },
)
