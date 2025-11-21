local I = import '../../specimens/lib.libsonnet';

// iss-005: Delete redundant on_response override in ConnectionManager

I.issueOneOccurrence(
  rationale=|||
    The `on_response` method in `ConnectionManager` (lines 167-168) is a redundant override
    that should be deleted.

    **Current code (lines 167-168):**
    ```python
    def on_response(self, evt: Any) -> None:
        return None
    ```

    **Why delete:**
    - ConnectionManager extends BaseHandler (line 52)
    - BaseHandler.on_response is already a no-op (handler.py:133-138):
      ```python
      def on_response(self, evt: Response) -> None:
          """Called after receiving a complete model response with usage stats.

          Default: no-op.
          """
          return
      ```
    - Overriding a no-op with another no-op is pointless
    - No callers found (grep showed 0 call sites)
    - Identical behavior whether override exists or not

    **What to delete:**
    Just remove lines 167-168 entirely. The base class implementation will be used.

    **Type note:**
    The override uses `evt: Any` while base class uses `evt: Response`. This suggests
    the override might have been added when types were unclear, but it's still doing nothing.

    **Benefits of deletion:**
    - Less code to maintain
    - Removes confusion about why override exists
    - Clearer that default behavior is being used
    - One less place to look when searching for on_response implementations
  |||,
  properties=['dead-code', 'redundant-override'],
  filesToRanges={
    'adgn/src/adgn/agent/server/runtime.py': [
      [167, 168],  // Redundant on_response override
      [52, 52],    // ConnectionManager class declaration (extends BaseHandler)
    ],
  },
)
