local I = import '../../specimens/lib.libsonnet';

I.issueOneOccurrence(
  rationale= |||
    The `get_approvals()` resource handler assigns `self.pending` to a local variable `pending_map` and then immediately iterates over it once:

    ```python
    async def get_approvals() -> ApprovalsResponse:
        """Get all approvals for this agent (pending + decided history)."""
        # Build pending approvals
        pending_map = self.pending  # Line 160
        pending_approvals = [
            ApprovalItem(
                call_id=call_id,
                tool_call=tool_call,
                status=ApprovalStatus.PENDING,
                reason=None,
                timestamp=datetime.now(),
            )
            for call_id, tool_call in pending_map.items()  # Line 169
        ]
    ```

    The `pending_map` variable is a one-off variable that adds no value:
    - It's assigned once from `self.pending`
    - It's used exactly once in the list comprehension
    - It doesn't add clarity or improve readability
    - It doesn't capture an intermediate computation

    This should directly use `self.pending.items()` in the list comprehension.

    **Fix:**
    Remove the intermediate variable and iterate directly:
    ```python
    async def get_approvals() -> ApprovalsResponse:
        """Get all approvals for this agent (pending + decided history)."""
        # Build pending approvals
        pending_approvals = [
            ApprovalItem(
                call_id=call_id,
                tool_call=tool_call,
                status=ApprovalStatus.PENDING,
                reason=None,
                timestamp=datetime.now(),
            )
            for call_id, tool_call in self.pending.items()
        ]
    ```

    This eliminates unnecessary indirection.
  |||,
  properties=['no-oneoff-vars-and-trivial-wrappers'],
  filesToRanges={
    'adgn/src/adgn/agent/approvals.py': [
      160,  // pending_map = self.pending
      169,  // for call_id, tool_call in pending_map.items()
    ],
  },
)
