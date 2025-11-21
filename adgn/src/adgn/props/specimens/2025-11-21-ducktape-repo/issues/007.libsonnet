local I = import '../../specimens/lib.libsonnet';

// iss-007: Remove redundant tuple construction - arguments already in FunctionCallItem

I.issueOneOccurrence(
  rationale=|||
    The code builds a list of tuples `(function_call, function_call.arguments)` when the
    `arguments` field is already part of the `FunctionCallItem` object. This is redundant
    data duplication.

    **Current code (lines 255-258):**
    ```python
    function_calls: list[FunctionCallItem] = list(self.pending_function_calls)
    calls: list[tuple[FunctionCallItem, str | None]] = [
        (function_call, function_call.arguments) for function_call in function_calls
    ]
    ```

    **Why it's redundant:**
    - `FunctionCallItem` already has `arguments: str | None` field (model.py:77)
    - The tuple just duplicates data that's already in the object
    - Both `_run_tool_calls_parallel` and `_run_tool_calls_sequential` immediately unpack
      the tuple and could just access `function_call.arguments` directly

    **Usage patterns:**

    Line 291: `await self._run_tool_calls_parallel(calls, function_calls, _invoke)`
    Line 293: `await self._run_tool_calls_sequential(calls, function_calls, _invoke)`

    Both methods receive BOTH `calls` (tuples) AND `function_calls` (original list).

    **Sequential usage (line 336):**
    ```python
    for i, (function_call, args_json) in enumerate(calls):
        outcome = await invoker(function_call, args_json)
    ```
    Could be:
    ```python
    for i, function_call in enumerate(function_calls):
        outcome = await invoker(function_call, function_call.arguments)
    ```

    **Parallel usage (line 305):**
    ```python
    async def runner(fc: FunctionCallItem, aj: str | None) -> None:
    ```
    Then line 310: `for fc, aj in calls:`

    Could be:
    ```python
    async def runner(fc: FunctionCallItem) -> None:
    ```
    Then line 310: `for fc in function_calls:`
    And access `fc.arguments` inside runner.

    **Correct approach:**
    1. Delete the `calls` tuple construction (lines 256-258)
    2. Update `_run_tool_calls_parallel` signature to take only `function_calls`
    3. Update `_run_tool_calls_sequential` signature to take only `function_calls`
    4. Access `function_call.arguments` directly in both methods
    5. Delete the tuple unpacking, iterate over `function_calls` directly

    **Benefits:**
    - No data duplication
    - Simpler code - iterate over the actual objects
    - One less list to construct
    - Clearer that we're working with FunctionCallItem objects
    - Less confusion about "why are there two lists?"

    **Note:**
    Both methods currently receive `calls` AND `function_calls`, which is further evidence
    of redundancy. The `function_calls` list is used for error handling/cleanup (line 340),
    while `calls` is iterated. This could all be done with just `function_calls`.
  |||,
  properties=['redundancy', 'data-duplication', 'simplicity'],
  filesToRanges={
    'adgn/src/adgn/agent/agent.py': [
      [255, 258],  // Redundant tuple construction
      [291, 291],  // Call to _run_tool_calls_parallel with both lists
      [293, 293],  // Call to _run_tool_calls_sequential with both lists
      [296, 298],  // _run_tool_calls_parallel signature
      [333, 335],  // _run_tool_calls_sequential signature
      [336, 336],  // Sequential unpacking of tuple
      [305, 305],  // Parallel runner signature
      [310, 310],  // Parallel iteration over calls
    ],
  },
)
