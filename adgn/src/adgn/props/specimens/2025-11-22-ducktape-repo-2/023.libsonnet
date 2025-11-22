{
  title: 'get_approvals() uses intermediate pending_map variable unnecessarily',
  severity: 'minor',
  category: 'code-style',
  locations: [
    {
      path: 'adgn/src/adgn/agent/approvals.py',
      lines: [160, 169],
      context: 'pending_map = self.pending then iterate over pending_map.items()',
    },
  ],
  description: |||
    The `get_approvals()` resource handler assigns `self.pending` to a local variable
    and then immediately iterates over it:

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

    The `pending_map` variable serves no purpose - it's assigned and used once.
    This should directly use `self.pending.items()`.
  |||,
  recommendation: |||
    Remove the `pending_map` variable and iterate directly:

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

        # ... rest of function
    ```

    This removes unnecessary indirection.
  |||,
}
