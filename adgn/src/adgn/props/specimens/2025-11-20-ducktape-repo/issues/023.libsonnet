local I = import '../../specimens/lib.libsonnet';

// iss-023: Inconsistent proposal_id type - converting int to str unnecessarily

I.issueOneOccurrence(
  rationale=|||
    The `notify_proposal_change` method and its callers have inconsistent proposal_id types.
    The method signature accepts `str`, but callers have `int` and must convert with `str()`,
    suggesting the type should be `int` consistently.

    **Current code:**

    Line 211: Method signature
    ```python
    def notify_proposal_change(self, proposal_id: str) -> None:
    ```

    Line 236: Caller in create_proposal
    ```python
    self.notify_proposal_change(str(new_id))  # new_id is int
    ```

    Line 253: Caller in approve_proposal
    ```python
    self.notify_proposal_change(str(proposal_id))  # proposal_id is int (line 239 signature)
    ```

    Line 258: Caller in reject_proposal
    ```python
    self.notify_proposal_change(str(proposal_id))  # proposal_id is int (line 255 signature)
    ```

    **The problem:**
    - All callers have `proposal_id` as `int` (see method signatures at lines 239, 255)
    - All callers must explicitly convert: `str(proposal_id)`
    - This suggests the method signature is wrong - it should accept `int`
    - Persistence layer likely uses `int` for proposal IDs
    - URI formatting at line 217 would work fine with int (f-string converts automatically)

    **Correct approach:**
    Change `notify_proposal_change` signature to accept `int`:
    ```python
    def notify_proposal_change(self, proposal_id: int) -> None:
        """Notify about a specific proposal change and the proposals index."""
        self.notify_resource(f"{APPROVAL_POLICY_PROPOSALS_INDEX_URI}/{proposal_id}")
        self.notify_proposals_changed()
        self.notify_resource(AGENTS_POLICY_STATE_URI_FMT.format(agent_id=self.agent_id))
    ```

    Then all callers can pass `int` directly without conversion:
    ```python
    self.notify_proposal_change(new_id)
    self.notify_proposal_change(proposal_id)
    self.notify_proposal_change(proposal_id)
    ```

    **Why this matters:**
    - Eliminates unnecessary type conversions
    - Makes type consistency clear
    - Aligns with persistence layer and method signatures
    - f-string at line 217 will handle int→str conversion automatically
    - Reduces cognitive load about "what's the real type here"

    **Related issues:**
    Connected to issue 022 about using wrong ID in create_proposal.
  |||,
  properties=['type-safety', 'api-design', 'consistency'],
  filesToRanges={
    'adgn/src/adgn/agent/approvals.py': [
      [211, 220],  // notify_proposal_change method with str signature
      [217, 217],  // f-string that would work fine with int
      [236, 236],  // Caller converting int to str
      [253, 253],  // Caller converting int to str
      [258, 258],  // Caller converting int to str
    ],
  },
)
