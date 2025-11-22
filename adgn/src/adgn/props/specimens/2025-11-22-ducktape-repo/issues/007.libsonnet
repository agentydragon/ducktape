local I = import '../../specimens/lib.libsonnet';

// iss-007: _invoke takes redundant args_json parameter (already in function_call.arguments)

I.issueOneOccurrence(
  rationale=|||
    The `_invoke` local function takes both `function_call: FunctionCallItem` and
    `args_json: str | None` as separate parameters, but `args_json` is always
    passed as `function_call.arguments`, making it redundant.

    **Current signature (line 261-265):**
    ```python
    async def _invoke(
        function_call: FunctionCallItem,
        args_json: str | None,
        local_map: dict[str, CallToolResult] = local_result_map,
    ) -> ToolCallOutcome:
    ```

    **Call sites always pass function_call.arguments:**
    ```python
    # Line 305 (parallel execution)
    outcome = await invoker(fc, fc.arguments)

    # Line 334 (sequential execution)
    outcome = await invoker(function_call, function_call.arguments)
    ```

    **Usage in _invoke body (lines 276-280):**
    ```python
    args: dict[str, Any] = {}
    if args_json:
        val = json.loads(args_json)
        if not isinstance(val, dict):
            raise ValueError("tool arguments must be a JSON object")
        args = val
    ```

    **Problems with redundant parameter:**
    1. **Data duplication**: Same data passed twice (once in object, once separately)
    2. **Cognitive load**: Reader must verify args_json matches function_call.arguments
    3. **Maintenance burden**: If FunctionCallItem structure changes, must update both
    4. **Potential inconsistency**: Nothing enforces args_json == function_call.arguments
    5. **Unnecessary coupling**: Invoker signature tied to call site's destructuring choice
    6. **Violates DRY**: Arguments already accessible via function_call object

    **Why it exists:**
    Likely historical artifact where:
    - Originally might have been called with different args_json than function_call.arguments
    - Or function_call object wasn't passed initially, only args were
    - Copy-paste from different context where separation made sense
    - Over-engineering for "flexibility" that's never used

    **Correct approach - Remove args_json parameter:**

    ```python
    async def _invoke(
        function_call: FunctionCallItem,
        local_map: dict[str, CallToolResult] = local_result_map,
    ) -> ToolCallOutcome:
        cid = _require_call_id(function_call)
        if cid in local_map:
            if (cached := copy.deepcopy(local_map[cid])).is_error:
                return ToolCallFailure(result=cached, reason=_maybe_error_message(cached))
            return ToolCallSuccess(result=cached)

        # Parse arguments from function_call directly
        args: dict[str, Any] = {}
        if function_call.arguments:  # Use function_call.arguments instead of args_json
            val = json.loads(function_call.arguments)
            if not isinstance(val, dict):
                raise ValueError("tool arguments must be a JSON object")
            args = val
        raw = await self._mcp_client.call_tool(function_call.name, args, raise_on_error=False)
        res = copy.deepcopy(raw)
        if res.is_error:
            return ToolCallFailure(result=res, reason=_maybe_error_message(res))
        return ToolCallSuccess(result=res)
    ```

    **Update call sites to pass only function_call:**

    ```python
    # Line 305 - parallel
    outcome = await invoker(fc)  # Remove fc.arguments

    # Line 334 - sequential
    outcome = await invoker(function_call)  # Remove function_call.arguments
    ```

    **Update method signatures:**

    ```python
    # Line 294
    async def _run_tool_calls_parallel(
        self, function_calls: list[FunctionCallItem], invoker
    ) -> None:
        # ...
        async def runner(fc: FunctionCallItem) -> None:
            # ...
            outcome = await invoker(fc)  # Single parameter

    # Line 331
    async def _run_tool_calls_sequential(
        self, function_calls: list[FunctionCallItem], invoker
    ) -> None:
        for i, function_call in enumerate(function_calls):
            outcome = await invoker(function_call)  # Single parameter
    ```

    **Benefits:**
    - **Single source of truth**: Arguments come from function_call object only
    - **Simpler signature**: One parameter instead of two
    - **Type safety**: Can't accidentally pass mismatched args_json
    - **Less code**: Fewer parameters to pass around
    - **Clearer intent**: "Invoke this function call" not "invoke with these args"
    - **Easier refactoring**: If FunctionCallItem changes, only one place to update

    **Alternative considered:**
    Keep args_json but make it Optional and default to function_call.arguments.
    Rejected because there's no use case for overriding - always want the arguments
    from the function call object.

    This is a classic case of unnecessary abstraction creating complexity without benefit.
  |||,
  properties=['api-design', 'simplicity', 'dry-principle', 'redundancy'],
  filesToRanges={
    'adgn/src/adgn/agent/agent.py': [
      [261, 265],  // _invoke signature with redundant args_json
      [276, 280],  // Usage of args_json (could use function_call.arguments)
      [305, 305],  // Call site passing fc.arguments redundantly
      [334, 334],  // Call site passing function_call.arguments redundantly
    ],
  },
)
