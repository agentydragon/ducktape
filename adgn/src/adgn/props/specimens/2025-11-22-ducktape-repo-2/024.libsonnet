local I = import '../../specimens/lib.libsonnet';

I.issueOneOccurrence(
  rationale= |||
    There are TWO separate enums representing the same concept - the status of a tool call approval:

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
    Used in the persistence layer for the same concept.

    This duplication requires a conversion function (lines 175-181):
    ```python
    def map_outcome_to_status(outcome: ApprovalOutcome) -> ApprovalStatus:
        """Map ApprovalOutcome to ApprovalStatus using value-based conversion."""
        try:
            return ApprovalStatus(outcome.value)
        except ValueError:
            # Fallback for unknown outcomes
            return ApprovalStatus.REJECTED  # HIDES ERRORS!
    ```

    **Multiple problems:**

    1. **Hides errors via silent fallback:** The converter falls back to REJECTED for unknown outcomes instead of failing fast. This masks data corruption or version mismatches.

    2. **Design smell:** If two enums need value-based conversion, they should be ONE enum. The existence of this converter indicates they represent the same domain concept but are duplicated across layers.

    3. **Maintenance burden:** Changes to approval statuses must be synchronized across both enums and the converter, creating opportunities for inconsistency.

    4. **Type imprecision:** Having two types for the same concept weakens type safety - conversions can silently succeed even when semantics diverge.

    **Fix:**
    Unify into a single enum shared between the API and persistence layers:
    ```python
    # In a shared module (e.g., adgn.agent.types)
    class ApprovalStatus(StrEnum):
        """Status of a tool call approval."""
        PENDING = "pending"
        APPROVED = "approved"
        REJECTED = "rejected"
        DENIED = "denied"
        ABORTED = "aborted"
    ```

    Use this single enum everywhere - in persistence, API responses, and the approval hub. If there's a genuine semantic difference between "outcome" and "status", document it clearly and create an explicit, exhaustive mapping that raises errors for unmapped values rather than hiding them with fallbacks.

    Benefits of unification:
    - No conversion needed
    - Single source of truth
    - Type errors caught at compile time
    - Simpler codebase
    - No silent error masking
  |||,
  properties=['type-correctness-and-specificity', 'python/no-swallowing-errors'],
  filesToRanges={
    'adgn/src/adgn/agent/approvals.py': [
      [70, 76],   // ApprovalStatus enum definition
      [175, 181], // map_outcome_to_status converter with error hiding
    ],
  },
  gap_note= |||
    This finding represents a generalizable anti-pattern: "duplicate domain enums across architectural layers requiring error-hiding converters."

    A property like "single-source-domain-types" could capture:
    - Domain concepts should have exactly one canonical type definition
    - Types should not be duplicated across layers (API, persistence, business logic)
    - Layer-crossing code should use the same types, not convert between duplicates
    - When conversion is truly needed (e.g., external API schema vs internal), converters must be explicit and fail-fast

    This is distinct from the existing "type-correctness-and-specificity" property which focuses on type precision/narrowness, not on avoiding duplication of domain type definitions.
  |||,
)
