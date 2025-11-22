local I = import '../../specimens/lib.libsonnet';

I.issueOneOccurrence(
  rationale= |||
    The invoker callback is called with both a FunctionCall object and its arguments as separate parameters:
    ```python
    outcome = await invoker(fc, fc.arguments)
    ```

    The second parameter `fc.arguments` is redundant because it can be trivially derived from the first parameter (fc.arguments). This violates DRY - the invoker should only need the FunctionCall object.

    This is essentially a form of unnecessary aliasing/renaming where the caller is extracting a field and passing it separately, forcing the callee to receive the same information twice. The invoker implementation should extract arguments internally when needed.

    **Fix:**
    Change the invoker signature to accept only the FunctionCall object:
    ```python
    outcome = await invoker(fc)
    ```

    Update the invoker implementation to extract arguments internally:
    ```python
    async def invoker(fc: FunctionCall) -> Outcome:
        arguments = fc.arguments
        # ... rest of logic
    ```

    This removes the redundant parameter and makes the API cleaner by avoiding unnecessary data extraction at the call site.
  |||,
  properties=['no-random-renames'],
  filesToRanges={
    'adgn/src/adgn/agent/agent.py': [
      305,  // outcome = await invoker(fc, fc.arguments)
    ],
  },
)
