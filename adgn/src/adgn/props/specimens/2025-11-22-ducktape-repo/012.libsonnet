local I = import '../../specimens/lib.libsonnet';

// iss-012: approvals_list should be built with list comprehensions, not imperative append

I.issueOneOccurrence(
  rationale=|||
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

    Fix - refactor to use list comprehensions:

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
  properties=['python/modern-python-idioms'],
  filesToRanges={
    'adgn/src/adgn/agent/mcp_bridge/servers/approvals_bridge.py': [
      [64, 65],   // approvals_list initialization and pending loop
      [71, 80],   // pending loop body
      [99, 108],  // decided approvals loop
    ],
  },
)
