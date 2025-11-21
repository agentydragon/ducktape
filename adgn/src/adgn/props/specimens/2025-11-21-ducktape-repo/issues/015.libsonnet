local I = import '../../specimens/lib.libsonnet';

// iss-015: proposals_list should use direct list comprehension with inlined proposals variable

I.issueOneOccurrence(
  rationale=|||
    The `proposals_list` resource handler should use a direct list comprehension instead of
    assigning the database query result to a `proposals` variable first.

    **Current code (lines 148-160):**
    ```python
    @self.resource(APPROVAL_POLICY_PROPOSALS_INDEX_URI + "/list", name="proposals_list", mime_type="application/json")
    async def proposals_list() -> list[ProposalDescriptor]:
        """List all policy proposals with status and timestamps."""
        proposals = await self._engine.persistence.list_policy_proposals(self._engine.agent_id)
        return [
            ProposalDescriptor(
                id=p.id,
                status=ProposalStatus(p.status),
                created_at=p.created_at,
                decided_at=p.decided_at,
            )
            for p in proposals
        ]
    ```

    **Why this is problematic:**
    - `proposals` variable is only used once in the list comprehension
    - Unnecessary intermediate variable that doesn't add clarity
    - Extra line of code that provides no value

    **Recommended fix:**
    Inline the database query directly into the list comprehension:
    ```python
    @self.resource(APPROVAL_POLICY_PROPOSALS_INDEX_URI + "/list", name="proposals_list", mime_type="application/json")
    async def proposals_list() -> list[ProposalDescriptor]:
        """List all policy proposals with status and timestamps."""
        return [
            ProposalDescriptor(
                id=p.id,
                status=ProposalStatus(p.status),
                created_at=p.created_at,
                decided_at=p.decided_at,
            )
            for p in await self._engine.persistence.list_policy_proposals(self._engine.agent_id)
        ]
    ```

    **Benefits:**
    - One less variable to track
    - More concise (11 lines → 10 lines)
    - Clearer that the query result is only used for the comprehension
    - Consistent with Python best practices for single-use iterables

    **Note:**
    The `await` expression in the comprehension is valid Python syntax and doesn't hurt readability.
  |||,
  properties=['simplicity', 'variable-usage'],
  filesToRanges={
    'adgn/src/adgn/mcp/approval_policy/server.py': [
      [148, 160],  // proposals_list with unnecessary proposals variable
      [151, 151],  // proposals variable assignment
    ],
  },
)
