local I = import '../../specimens/lib.libsonnet';

// iss-016: Use walrus operator for got variable in proposal_detail

I.issueOneOccurrence(
  rationale=|||
    The `proposal_detail` resource handler should use the walrus operator (`:=`) to combine
    the assignment and None check of the `got` variable into a single expression.

    **Current code (lines 165-167):**
    ```python
    got = await self._engine.persistence.get_policy_proposal(self._engine.agent_id, id)
    if got is None:
        raise KeyError(id)
    ```

    **Why this should be improved:**
    - The pattern "assign, then immediately check for None" is verbose
    - Python 3.8+ walrus operator allows combining assignment with conditional
    - Reduces visual noise and makes the None-check pattern more obvious
    - Common Python idiom for this pattern

    **Recommended fix:**
    Use walrus operator to combine assignment and check:
    ```python
    if (got := await self._engine.persistence.get_policy_proposal(self._engine.agent_id, id)) is None:
        raise KeyError(id)
    ```

    **Benefits:**
    - More concise (3 lines → 2 lines)
    - Emphasizes that this is a "fetch-or-error" pattern
    - Eliminates the gap between assignment and usage
    - Modern Python idiom widely used in the codebase

    **Note:**
    This is a simple readability improvement that doesn't change behavior.
  |||,
  properties=['modern-python', 'conciseness', 'idioms'],
  filesToRanges={
    'adgn/src/adgn/mcp/approval_policy/server.py': [
      [165, 167],  // got variable assignment and None check
    ],
  },
)
