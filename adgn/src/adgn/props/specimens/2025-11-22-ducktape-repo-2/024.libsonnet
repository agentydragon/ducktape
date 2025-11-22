{
  title: 'ApprovalOutcome and ApprovalStatus are duplicate enums that should be unified',
  severity: 'medium',
  category: 'type-design',
  locations: [
    {
      path: 'adgn/src/adgn/agent/approvals.py',
      lines: [70, 76],
      context: 'ApprovalStatus enum definition',
    },
    {
      path: 'adgn/src/adgn/agent/persist/__init__.py',
      lines: null,
      context: 'ApprovalOutcome enum definition (referenced)',
    },
    {
      path: 'adgn/src/adgn/agent/approvals.py',
      lines: [175, 181],
      context: 'map_outcome_to_status conversion function',
    },
  ],
  description: |||
    There are TWO separate enums representing the status of a tool call approval:

    **1. ApprovalStatus (in approvals.py, lines 70-76):**
    ```python
    class ApprovalStatus(StrEnum):
        """Status of an approval (pending or decided)."""
        PENDING = "pending"
        APPROVED = "approved"
        REJECTED = "rejected"
        DENIED = "denied"
        ABORTED = "aborted"
    ```

    **2. ApprovalOutcome (in persist/__init__.py):**
    Used in persistence layer for the same concept.

    **The problem:**
    This requires conversion between the two (lines 175-181):
    ```python
    def map_outcome_to_status(outcome: ApprovalOutcome) -> ApprovalStatus:
        """Map ApprovalOutcome to ApprovalStatus using value-based conversion."""
        try:
            return ApprovalStatus(outcome.value)
        except ValueError:
            # Fallback for unknown outcomes
            return ApprovalStatus.REJECTED  # HIDES ERRORS!
    ```

    This converter:
    1. **Hides errors** - Falls back to REJECTED for unknown outcomes instead of failing fast
    2. **Indicates design smell** - If two enums need value-based conversion, they should be ONE enum
    3. **Creates maintenance burden** - Changes to one enum must be synchronized with the other

    There should NOT be two separate enums encoding the same concept.
  |||,
  recommendation: |||
    **Unify ApprovalOutcome and ApprovalStatus into a single enum:**

    **Option 1: Keep ApprovalStatus, remove ApprovalOutcome**
    ```python
    # In a shared module (e.g., adgn.agent.types)
    class ApprovalStatus(StrEnum):
        """Status of a tool call approval."""
        PENDING = "pending"
        APPROVED = "approved"
        REJECTED = "rejected"
        DENIED = "denied"
        ABORTED = "aborted"
        # Add any states from ApprovalOutcome that differ
    ```

    Use this single enum everywhere - in persistence, in API responses, in the approval hub.

    **Option 2: If there's a semantic difference**
    If ApprovalOutcome genuinely represents something different from ApprovalStatus
    (e.g., outcomes have more granular states than statuses), document this clearly
    and create an explicit mapping function that:
    1. **Does NOT hide errors** - removes the try/except fallback
    2. **Has exhaustive pattern matching** - uses match/case to ensure all outcomes are handled
    3. **Fails fast** - raises error for unmapped values

    But most likely, these two enums represent the same concept and should be unified.

    **Benefits of unification:**
    - No conversion needed
    - Single source of truth
    - Errors caught at compile time (type checking)
    - Simpler codebase
  |||,
}
