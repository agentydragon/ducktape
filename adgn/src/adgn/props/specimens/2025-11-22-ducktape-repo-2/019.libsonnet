{
  title: 'invoker callback takes redundant fc.arguments parameter',
  severity: 'minor',
  category: 'api-design',
  locations: [
    {
      path: 'adgn/src/adgn/agent/agent.py',
      lines: [305],
      context: 'outcome = await invoker(fc, fc.arguments)',
    },
  ],
  description: |||
    The `invoker` callback is called with both a FunctionCall object and its arguments:

    ```python
    outcome = await invoker(fc, fc.arguments)
    ```

    This is redundant - `fc.arguments` is trivially derivable from `fc` (it's just `fc.arguments`).
    The invoker should only need the FunctionCall object.

    **Same pattern appears elsewhere:**
    ```python
    await invoker(function_call, function_call.arguments)
    ```

    This violates DRY - the second parameter is always extractable from the first.
  |||,
  recommendation: |||
    Change the invoker signature to take only the FunctionCall:

    **Before:**
    ```python
    outcome = await invoker(fc, fc.arguments)
    ```

    **After:**
    ```python
    outcome = await invoker(fc)
    ```

    Update the invoker implementation to extract arguments internally:
    ```python
    async def invoker(fc: FunctionCall) -> Outcome:
        # Extract arguments from fc
        arguments = fc.arguments
        # ... rest of logic
    ```

    This removes the redundant parameter and makes the API cleaner.
  |||,
}
