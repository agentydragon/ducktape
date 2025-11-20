local I = import '../../specimens/lib.libsonnet';

// iss-015: Use walrus operator to inline models assignment in __getattr__

I.issueOneOccurrence(
  rationale=|||
    The `__getattr__` method extracts `models` on line 205 and then uses it immediately
    on line 206 in the conditional check. This should use the walrus operator to inline
    the assignment.

    **Current code (lines 205-207):**
    ```python
    models = self._models.get(name)
    if not models:
        raise AttributeError(name)
    ```

    **Should be:**
    ```python
    if not (models := self._models.get(name)):
        raise AttributeError(name)
    ```

    **Why inline with walrus operator?**
    - Combines retrieval and check into one line
    - models remains available for subsequent use (line 208)
    - More concise without sacrificing readability
    - Standard Python 3.8+ pattern for "get and check" scenarios
    - Consistent with similar patterns elsewhere in the codebase

    **Note:**
    Same pattern appears in the `error` method at lines 181-183, which could also benefit
    from the same refactor, though that's less critical since the `error` method overall
    is simpler.
  |||,
  properties=['code-style', 'readability'],
  filesToRanges={
    'adgn/src/adgn/mcp/stubs/typed_stubs.py': [
      [205, 207],  // models assignment and check in __getattr__
      [181, 183],  // Same pattern in error method (secondary)
    ],
  },
)
