{
  title: 'approvals_list should be built with list comprehensions, not imperative append',
  severity: 'minor',
  category: 'pythonic',
  locations: [
    {
      path: 'adgn/src/adgn/agent/mcp_bridge/servers/approvals_bridge.py',
      lines: [64, 65, 71, 79, 80, 99, 107, 108],
      context: 'Building approvals_list in get_approvals resource',
    },
  ],
  description: |||
    The `approvals_list` is built imperatively using `append()` in two separate loops:

    ```python
    approvals_list = []
    pending_count = 0
    decided_count = 0

    # Add pending approvals
    for call_id, tool_call in pending_map.items():
        approvals_list.append(ApprovalItem(...))
        pending_count += 1

    # Add decided approvals from persistence
    for record in records:
        if record.decision is not None:
            # ... if-else chain ...
            approvals_list.append(ApprovalItem(...))
            decided_count += 1
    ```

    This is non-idiomatic Python. List comprehensions are more concise, readable, and
    Pythonic for building lists from iterables.
  |||,
  recommendation: |||
    Refactor to use list comprehensions:

    ```python
    # Pending approvals
    pending_approvals = [
        ApprovalItem(
            call_id=call_id,
            tool_call=tool_call,
            status=ApprovalStatus.PENDING,
            reason=None,
            timestamp=datetime.now(),
        )
        for call_id, tool_call in self._hub.pending.items()
    ]

    # Decided approvals
    decided_approvals = [
        ApprovalItem(
            call_id=record.tool_call.id,
            tool_call=record.tool_call,
            status=_map_outcome_to_status(record.decision.outcome),
            reason=record.decision.reason,
            timestamp=record.decision.decided_at,
        )
        for record in await self._persistence.get_tool_call_records(self._agent_id)
        if record.decision is not None
    ]

    # Combine and sort
    approvals_list = sorted(
        pending_approvals + decided_approvals,
        key=lambda x: x.timestamp,
        reverse=True
    )
    ```

    This eliminates manual counting (addressed in finding 010) and the imperative style.
  |||,
}
