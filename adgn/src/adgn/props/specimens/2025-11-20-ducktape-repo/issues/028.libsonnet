local I = import '../../specimens/lib.libsonnet';

// iss-028: Use ternary operator for policy_source assignment

I.issueOneOccurrence(
  rationale=|||
    The code initializes policy_source to None and then conditionally assigns a value.
    This should use a ternary operator for conciseness.

    **Current code (lines 88-90):**
    ```python
    policy_source = None
    if initial_policy:
        policy_source = initial_policy.read_text()
    ```

    **Should be:**
    ```python
    policy_source = initial_policy.read_text() if initial_policy else None
    ```

    **Why ternary is better:**
    - One line instead of three
    - More concise and readable
    - Clearly expresses the conditional assignment pattern
    - Standard Python idiom for simple conditional values
    - Easier to see both branches at once

    **Pattern applicability:**
    This is a classic ternary operator use case: simple conditional assignment where
    one branch has a value and the other is None (or another default).

    **Type safety:**
    Both versions correctly type as `str | None`. The ternary makes the two possible
    values (read_text() result or None) more visually apparent.
  |||,
  properties=['code-style', 'readability'],
  filesToRanges={
    'adgn/src/adgn/agent/mcp_bridge/cli.py': [
      [88, 90],  // policy_source conditional assignment
    ],
  },
)
