local I = import '../../specimens/lib.libsonnet';

// iss-022: create_proposal notifies with wrong ID (placeholder instead of actual)

I.issueOneOccurrence(
  rationale=|||
    The `create_proposal` method notifies with a placeholder ID (0) instead of the actual ID
    returned by the persistence layer. This is a bug.

    **Current code (lines 232-236):**
    ```python
    new_id = 0  # Placeholder, actual ID assigned by database
    await self.persistence.create_policy_proposal(self.agent_id, proposal_id=new_id, content=content)
    # Note: We don't have the actual ID here, but persistence will handle it
    # For now, notify with string version for compatibility
    self.notify_proposal_change(str(new_id))
    return new_id
    ```

    **The bug:**
    - Line 232 sets `new_id = 0` as a placeholder
    - Line 233 calls `create_policy_proposal` with `proposal_id=new_id` (0)
    - Line 236 notifies with `str(new_id)` which is still "0"
    - The actual database-assigned ID is never retrieved or used
    - Comment at line 234-235 acknowledges this: "We don't have the actual ID here"

    **Why this is wrong:**
    - Clients receiving the notification will get the wrong proposal ID
    - The notification points to a non-existent proposal (ID 0)
    - Return value at line 237 is also wrong (returns 0 instead of actual ID)
    - Creates data inconsistency between what's notified and what's persisted

    **Correct approach:**
    The `create_policy_proposal` method should return the actual database-assigned ID:
    ```python
    new_id = await self.persistence.create_policy_proposal(self.agent_id, proposal_id=0, content=content)
    self.notify_proposal_change(str(new_id))  # Now notifies with actual ID
    return new_id  # Now returns actual ID
    ```

    Or if the persistence method doesn't return the ID, it should be refactored to do so,
    or the code should query for the newly created proposal to get its ID.

    **Related issues:**
    This is connected to issue 023 about proposal_id type inconsistency.
  |||,
  properties=['correctness', 'bug', 'data-consistency'],
  filesToRanges={
    'adgn/src/adgn/agent/approvals.py': [
      [232, 237],  // create_proposal using placeholder ID 0
      [234, 235],  // Comment acknowledging the problem
    ],
  },
)
