local I = import '../../specimens/lib.libsonnet';

// iss-009: _convert_pending_approvals should use list comprehension

I.issueOneOccurrence(
  rationale=|||
    The `_convert_pending_approvals` function uses an imperative loop-and-append
    pattern when a list comprehension would be clearer and more Pythonic.

    **Current code (lines 50-59):**
    ```python
    def _convert_pending_approvals(pending_map: dict[str, ToolCall]) -> list[PendingApproval]:
        result: list[PendingApproval] = []
        for _call_id, tool_call in pending_map.items():
            result.append(
                PendingApproval(
                    tool_call=tool_call,
                    timestamp=datetime.now(),  # TODO: Track creation time in PendingApproval or separately
                )
            )
        return result
    ```

    **Problems with imperative style:**
    1. **Verbose**: 9 lines for what should be 7 (or 5 if compact)
    2. **Mutable state**: `result` list is mutated via append
    3. **Less Pythonic**: List comprehensions preferred for simple transformations
    4. **Unused variable**: `_call_id` underscore-prefixed but still appears in code
    5. **Intent unclear**: Reader must parse loop to see it's just a map operation
    6. **Naming overhead**: `result` variable adds no semantic value

    **Correct approach with list comprehension:**
    ```python
    def _convert_pending_approvals(pending_map: dict[str, ToolCall]) -> list[PendingApproval]:
        return [
            PendingApproval(
                tool_call=tool_call,
                timestamp=datetime.now(),  # TODO: Track creation time in PendingApproval or separately
            )
            for tool_call in pending_map.values()
        ]
    ```

    **Benefits:**
    1. **More concise**: 7 lines instead of 9
    2. **Immutable**: No intermediate list mutation
    3. **Pythonic**: Idiomatic Python for transformations
    4. **Clearer intent**: "Return list of PendingApprovals built from tool_calls"
    5. **No unused variables**: Directly iterate values (call_id not needed)
    6. **Type inference**: Return type obvious from comprehension

    **Additional improvement - Don't need call_id:**
    The current code iterates `.items()` to get `(call_id, tool_call)` pairs but
    only uses `tool_call`. Since `call_id` is unused (underscore-prefixed), should
    iterate `.values()` directly:

    ```python
    # Current (unnecessary)
    for _call_id, tool_call in pending_map.items():

    # Better
    for tool_call in pending_map.values()
    ```

    This makes the comprehension even cleaner and signals that call_id is not needed.

    **When NOT to use list comprehension:**
    - Loop body has side effects (not the case - just constructs objects)
    - Complex control flow (not the case - simple 1:1 mapping)
    - Need to accumulate state across iterations (not the case - independent items)
    - Readability suffers from nested comprehensions (not the case - single level)

    None of these apply here. This is a textbook case for list comprehension:
    simple 1:1 transformation of input items to output items.

    **Python style guide (PEP 8):**
    List comprehensions are preferred over map() and filter() for simple cases,
    and over loop-and-append for simple transformations. This aligns with the
    Pythonic principle of "Simple is better than complex."
  |||,
  properties=['code-style', 'python-idioms', 'functional-programming', 'conciseness'],
  filesToRanges={
    'adgn/src/adgn/agent/mcp_bridge/servers/agents.py': [
      [50, 59],   // Loop-and-append pattern that should be list comprehension
      [52, 52],   // Iterates .items() but doesn't use call_id (should use .values())
    ],
  },
)
