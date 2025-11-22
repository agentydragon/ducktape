local I = import '../../specimens/lib.libsonnet';

// iss-047: Duplicated ToolCallRecord lifecycle states documentation

I.issueOneOccurrence(
  rationale=|||
    The ToolCallRecord lifecycle states are documented in two places with identical text:

    1. In the ToolCallRecord class docstring (lines 104-109):
    ```python
    class ToolCallRecord(BaseModel):
        """Complete tool call record from policy gate (tracks ALL calls through gate).

        States:
        - PENDING: decision=None, execution=None
        - EXECUTING: decision!=None, execution=None
        - COMPLETED: decision!=None, execution!=None
        """
    ```

    2. In the save_tool_call method docstring (lines 167-171):
    ```python
    async def save_tool_call(self, record: ToolCallRecord) -> None:
        """Save or update a tool call record (INSERT OR REPLACE).

        Use this for all lifecycle stages:
        - PENDING: decision=None, execution=None
        - EXECUTING: decision!=None, execution=None
        - COMPLETED: decision!=None, execution!=None
        """
    ```

    This violates the DRY (Don't Repeat Yourself) principle. The lifecycle states are a
    property of the ToolCallRecord type itself, not specific to the save_tool_call method.

    Fix: Document the lifecycle states only once in the ToolCallRecord class docstring.
    The save_tool_call method should either:
    - Remove the lifecycle documentation entirely (since it's on the type), or
    - Reference it briefly: "See ToolCallRecord for lifecycle stages"

    This ensures single source of truth and prevents documentation drift.
  |||,
  properties=['no-useless-docs'],
  filesToRanges={
    'adgn/src/adgn/agent/persist/__init__.py': [
      [104, 109],  // ToolCallRecord class docstring with states
      [167, 171],  // save_tool_call method docstring duplicating states
    ],
  },
)
