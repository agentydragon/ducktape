local I = import '../../specimens/lib.libsonnet';

// iss-002: ApprovalStatus unification lost policy vs user decision source information

I.issueOneOccurrence(
  rationale=|||
    Fix #024 unified ApprovalOutcome and ApprovalStatus into a single enum, but this was
    actually a **regression** that lost important semantic information.

    **What was lost:**

    The old ApprovalOutcome enum distinguished between:
    - POLICY_ALLOW vs USER_APPROVE
    - POLICY_DENY_CONTINUE vs USER_DENY_CONTINUE
    - POLICY_DENY_ABORT vs USER_DENY_ABORT

    This captured **WHY** a decision was made:
    - Was it automatically approved by policy?
    - Or did a human explicitly approve it after being asked?

    The new unified ApprovalStatus only has:
    - APPROVED (but was it policy or user?)
    - DENIED (but was it policy or user?)
    - ABORTED (but was it policy or user?)

    **Why this matters:**

    1. **Audit trails**: Can't distinguish "policy auto-approved risky action" from
       "user Alice explicitly approved risky action"

    2. **Analytics**: Can't measure how often policies auto-approve vs requiring user review

    3. **Debugging**: Can't tell if a tool executed because policy allowed it or because
       user overrode a deny-with-ask policy

    4. **Compliance**: Some regulations require knowing if a human reviewed a decision

    **The original issue diagnosis was wrong:**

    Issue #024 claimed ApprovalOutcome and ApprovalStatus were "duplicate domain enums"
    that needed value-based conversion. But they actually captured orthogonal information:
    - **What** was decided (approved/denied/aborted)
    - **Who** decided it (policy/user)

    The existence of a converter didn't mean they were duplicates - it meant they served
    different purposes and needed coordination.

    **The proper fix:**

    Don't merge the enums. Instead, fix the error-hiding converter and make the relationship
    explicit:

    **Option 1: Add source field to Decision**
    ```python
    class DecisionSource(StrEnum):
        POLICY = "policy"
        USER = "user"

    class Decision(BaseModel):
        outcome: ApprovalStatus      # What: APPROVED, DENIED, ABORTED
        source: DecisionSource        # Who: POLICY or USER
        decided_at: datetime
        reason: str | None
    ```

    **Option 2: More specific enum values (preserve old semantics)**
    ```python
    class ApprovalStatus(StrEnum):
        PENDING = "pending"
        POLICY_APPROVED = "policy_approved"
        USER_APPROVED = "user_approved"
        POLICY_DENIED = "policy_denied"
        USER_DENIED = "user_denied"
        POLICY_ABORTED = "policy_aborted"
        USER_ABORTED = "user_aborted"
    ```

    Either way:
    - Preserve the semantic distinction
    - Remove error-hiding fallbacks in converters
    - Make the mapping explicit and exhaustive
    - Fail fast on unknown values

    **Migration path:**

    For existing records with old ApprovalOutcome values, the migration is straightforward:
    ```python
    # Old → New (Option 1: with source field)
    POLICY_ALLOW → (APPROVED, POLICY)
    USER_APPROVE → (APPROVED, USER)
    POLICY_DENY_CONTINUE → (DENIED, POLICY)
    USER_DENY_CONTINUE → (DENIED, USER)
    POLICY_DENY_ABORT → (ABORTED, POLICY)
    USER_DENY_ABORT → (ABORTED, USER)

    # Old → New (Option 2: specific values)
    POLICY_ALLOW → POLICY_APPROVED
    USER_APPROVE → USER_APPROVED
    (etc.)
    ```

    **Current impact:**

    All new Decision records lose the policy/user distinction. Historical data (if it exists
    with old enum values) can't be loaded without a migration.
  |||,
  properties=['truthfulness', 'type-correctness-and-specificity'],
  filesToRanges={
    'adgn/src/adgn/agent/persist/__init__.py': [
      [76, 81],  // Decision class - now using unified ApprovalStatus
    ],
    'adgn/src/adgn/agent/types.py': [
      [11, 17],  // ApprovalStatus enum - unified without source distinction
    ],
    'adgn/src/adgn/mcp/policy_gateway/middleware.py': [
      [179, 188],  // ALLOW → APPROVED mapping (loses POLICY_ prefix)
      [262, 271],  // DENY_ABORT → ABORTED mapping
      [277, 286],  // DENY_CONTINUE → DENIED mapping
      [301, 310],  // User approve → APPROVED mapping (loses USER_ prefix)
      [336, 347],  // User deny → ABORTED mapping
    ],
  },
  gap_note=|||
    This represents a broader principle: "preserve-semantic-distinctions-in-domain-model"

    When unifying types, verify that you're not losing semantic information:
    - If two enums/types capture different aspects of the same concept, they might not be duplicates
    - The existence of a converter doesn't automatically mean redundancy
    - Before merging, ask: "What information would be lost?"
    - Some apparent duplication is actually orthogonal concerns (what vs who, when vs why, etc.)

    Related to "truthfulness" (data should accurately represent reality) and
    "type-correctness-and-specificity" (types should capture all relevant distinctions).

    The error-hiding converter was a real problem, but the solution should have been to fix
    the converter's fallback logic, not to merge the enums.
  |||,
)
