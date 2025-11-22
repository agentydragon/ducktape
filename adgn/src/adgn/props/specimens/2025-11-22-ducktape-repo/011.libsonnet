{
  title: '_register_resources contains identity mapping encoded as long if-else chain',
  severity: 'minor',
  category: 'maintainability',
  locations: [
    {
      path: 'adgn/src/adgn/agent/mcp_bridge/servers/approvals_bridge.py',
      lines: [86, 87, 88, 89, 90, 91, 92, 93, 94, 95, 96, 97],
      context: 'ApprovalOutcome to ApprovalStatus mapping in get_approvals resource',
    },
  ],
  description: |||
    The `get_approvals` resource function in `_register_resources()` contains a verbose
    if-elif-else chain that maps `ApprovalOutcome` enum values to `ApprovalStatus` enum
    values with identical names:

    ```python
    if record.decision.outcome == ApprovalOutcome.APPROVED:
        status = ApprovalStatus.APPROVED
    elif record.decision.outcome == ApprovalOutcome.REJECTED:
        status = ApprovalStatus.REJECTED
    elif record.decision.outcome == ApprovalOutcome.DENIED:
        status = ApprovalStatus.DENIED
    elif record.decision.outcome == ApprovalOutcome.ABORTED:
        status = ApprovalStatus.ABORTED
    else:
        status = ApprovalStatus.REJECTED
    ```

    This is an identity mapping (same name → same name) encoded verbosely. It suggests
    either:
    1. The two enums should be unified (ApprovalOutcome and ApprovalStatus are duplicates)
    2. Or the mapping should use the enum value directly: `ApprovalStatus(outcome.value)`
  |||,
  recommendation: |||
    Either:

    **Option 1 (preferred)**: Unify the enums if they represent the same concept.
    See finding 005 for a similar enum duplication issue.

    **Option 2**: Use value-based conversion:
    ```python
    try:
        status = ApprovalStatus(record.decision.outcome.value)
    except ValueError:
        status = ApprovalStatus.REJECTED  # Fallback
    ```

    **Option 3**: Use a simple dict mapping if the enums must remain separate:
    ```python
    OUTCOME_TO_STATUS = {
        ApprovalOutcome.APPROVED: ApprovalStatus.APPROVED,
        ApprovalOutcome.REJECTED: ApprovalStatus.REJECTED,
        ApprovalOutcome.DENIED: ApprovalStatus.DENIED,
        ApprovalOutcome.ABORTED: ApprovalStatus.ABORTED,
    }
    status = OUTCOME_TO_STATUS.get(record.decision.outcome, ApprovalStatus.REJECTED)
    ```
  |||,
}
