local I = import '../../specimens/lib.libsonnet';

// iss-013: Inline pending_record variable in PolicyGatewayMiddleware

I.issueOneOccurrence(
  rationale=|||
    The `pending_record` variable in `on_call_tool` method is only used once and should be inlined
    to reduce unnecessary local variable assignments.

    **Current code (lines 150-158):**
    ```python
    pending_record = ToolCallRecord(
        call_id=call_id,
        run_id=str(self._run_id) if self._run_id is not None else None,
        agent_id=self._agent_id,
        tool_call=tool_call,
        decision=None,
        execution=None,
    )
    await self._persistence.save_tool_call(pending_record)
    ```

    **Why this is problematic:**
    - `pending_record` is only used once on line 158 for the save call
    - Creates unnecessary intermediate variable that doesn't clarify the code
    - Adds cognitive overhead without providing value

    **Recommended fix:**
    Inline the construction directly into the save call:
    ```python
    await self._persistence.save_tool_call(
        ToolCallRecord(
            call_id=call_id,
            run_id=str(self._run_id) if self._run_id is not None else None,
            agent_id=self._agent_id,
            tool_call=tool_call,
            decision=None,
            execution=None,
        )
    )
    ```

    **Benefits:**
    - One less variable to track
    - Clearer that this record is created solely for the save operation
    - Consistent with how other temporary objects are used in the codebase

    **Note:**
    This is a simple refactoring that doesn't change behavior. The variable serves no
    documentation or reuse purpose.
  |||,
  properties=['simplicity', 'variable-usage'],
  filesToRanges={
    'adgn/src/adgn/mcp/policy_gateway/middleware.py': [
      [150, 158],  // pending_record creation and single use
    ],
  },
)
