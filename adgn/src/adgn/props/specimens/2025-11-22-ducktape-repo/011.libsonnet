local I = import '../../specimens/lib.libsonnet';

// iss-011: _register_resources contains identity mapping encoded as long if-else chain

I.issueOneOccurrence(
  rationale=|||
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

    Fix options:

    **Option 1 (preferred)**: Unify the enums if they represent the same concept.
    See finding 024 for a similar enum duplication issue.

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
  properties=['type-correctness-and-specificity', 'python/modern-python-idioms'],
  filesToRanges={
    'adgn/src/adgn/agent/mcp_bridge/servers/approvals_bridge.py': [
      [86, 97], // if-elif-else chain for ApprovalOutcome to ApprovalStatus mapping
    ],
  },
  gap_note=|||
    This pattern deserves a property like "single-source-domain-types": when the same
    domain concept is represented by multiple types requiring conversion between them,
    they should be unified into a single authoritative type. This is distinct from
    general "type-correctness-and-specificity" as it specifically addresses type
    proliferation and unnecessary conversions in domain modeling.
  |||,
)
