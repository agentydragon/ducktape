{
  title: 'approvals_list variable should be inlined',
  severity: 'minor',
  category: 'code-style',
  locations: [
    {
      path: 'adgn/src/adgn/agent/approvals.py',
      lines: [196, 204],
      context: 'approvals_list assigned and immediately returned',
    },
  ],
  description: |||
    The `approvals_list` variable is assigned and immediately used as the only
    argument to the return statement:

    ```python
    # Combine and sort by timestamp (most recent first)
    approvals_list = sorted(
        pending_approvals + decided_approvals,
        key=lambda x: x.timestamp,
        reverse=True,
    )

    return ApprovalsResponse(
        agent_id=self._agent_id,
        approvals=approvals_list,  # Used once
    )
    ```

    The variable serves no purpose - it's not reused or referenced elsewhere.
  |||,
  recommendation: |||
    Inline the sorted() call directly in the return statement:

    ```python
    # Combine and sort by timestamp (most recent first)
    return ApprovalsResponse(
        agent_id=self._agent_id,
        approvals=sorted(
            pending_approvals + decided_approvals,
            key=lambda x: x.timestamp,
            reverse=True,
        ),
    )
    ```

    This removes the unnecessary intermediate variable.
  |||,
}
