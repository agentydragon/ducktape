local I = import '../../specimens/lib.libsonnet';

// iss-005: PolicyStatus and ProposalStatus are duplicate enums causing type confusion

I.issueOneOccurrence(
  rationale=|||
    There are TWO separate enums for policy status that should be unified into one.
    The codebase mixes both enums inconsistently, relying on runtime string conversion
    to mask type mismatches. This creates type safety issues and semantic confusion.

    **PolicyStatus** (persist/__init__.py lines 54-58, models.py lines 39-43):
    ```python
    class PolicyStatus(StrEnum):
        ACTIVE = "active"
        SUPERSEDED = "superseded"
        PROPOSED = "proposed"
        REJECTED = "rejected"
    ```

    **ProposalStatus** (models/proposal_status.py lines 6-10):
    ```python
    class ProposalStatus(StrEnum):
        PENDING = "pending"
        APPROVED = "approved"
        REJECTED = "rejected"
        ERROR = "error"
    ```

    **Database schema uses PolicyStatus:**
    ```python
    # models.py line 176
    class Policy(Base):
        status: Mapped[PolicyStatus] = mapped_column(String, nullable=False)
    ```

    **But persistence operations use BOTH enums inconsistently:**

    ```python
    # sqlite.py line 217 - Creates with ProposalStatus.PENDING (WRONG TYPE!)
    policy = Policy(
        agent_id=agent_id,
        content=content,
        status=ProposalStatus.PENDING,  # Should be PolicyStatus!
    )

    # sqlite.py line 231 - Filters with ProposalStatus values (WRONG TYPE!)
    .where(Policy.agent_id == agent_id, Policy.status.in_([
        ProposalStatus.PENDING,
        ProposalStatus.APPROVED,
        ProposalStatus.REJECTED
    ]))

    # sqlite.py line 283 - Approves with PolicyStatus.ACTIVE (CORRECT)
    policy.status = PolicyStatus.ACTIVE

    # sqlite.py line 293 - Rejects with ProposalStatus.REJECTED (WRONG TYPE!)
    .values(status=ProposalStatus.REJECTED, decided_at=_now())
    ```

    **Runtime conversion masks the type mismatch:**
    ```python
    # approval_policy_bridge.py line 76
    ProposalDescriptor(
        id=p.id,
        status=ProposalStatus(p.status),  # Converting PolicyStatus → ProposalStatus!
        created_at=p.created_at,
    )
    ```

    This works at runtime because StrEnum values are strings and "rejected" == "rejected",
    but the type checker doesn't catch that we're mixing incompatible enums.

    **Problems with duplicate enums:**
    1. **Type confusion**: Same concept (policy status) has two incompatible types
    2. **Lost type safety**: Runtime conversion bypasses type checking
    3. **Semantic mismatch**: PENDING vs PROPOSED, APPROVED vs ACTIVE - which is correct?
    4. **Dead code**: ProposalStatus.APPROVED is never set, only used in filters
    5. **Inconsistent persistence**: Same entity uses different enums in different methods
    6. **Maintenance burden**: Changes require updating two enums and conversion code
    7. **Error prone**: Easy to use wrong enum, silently works due to string overlap

    **Actual lifecycle (discovered from code):**
    1. Create proposal: status=ProposalStatus.PENDING (should be PolicyStatus.PROPOSED)
    2. Approve proposal: status=PolicyStatus.ACTIVE
    3. Old active policy: status=PolicyStatus.SUPERSEDED
    4. Reject proposal: status=ProposalStatus.REJECTED (should be PolicyStatus.REJECTED)

    Note: ProposalStatus.APPROVED is NEVER set anywhere, only filtered for!
    Note: PolicyStatus.PROPOSED is NEVER used, but ProposalStatus.PENDING is!

    **Why duplication exists:**
    - PolicyStatus was likely the original enum in persist/__init__.py
    - ProposalStatus was created later in models/proposal_status.py for API responses
    - Duplication happened because models.py re-declares PolicyStatus to avoid circular imports
    - Different semantics evolved: PROPOSED vs PENDING, APPROVED vs ACTIVE

    **Correct approach - Single unified enum:**

    Create one canonical enum in a shared location (e.g., agent/models/policy_status.py):

    ```python
    class PolicyStatus(StrEnum):
        """Full lifecycle status for approval policies.

        Lifecycle:
        - PENDING: Proposal created, awaiting decision
        - ACTIVE: Currently active policy
        - SUPERSEDED: Was active, replaced by newer policy
        - REJECTED: Proposal rejected
        - ERROR: Proposal failed validation (reserved for future use)
        """
        PENDING = "pending"      # Was: ProposalStatus.PENDING
        ACTIVE = "active"        # Was: PolicyStatus.ACTIVE
        SUPERSEDED = "superseded"  # Was: PolicyStatus.SUPERSEDED
        REJECTED = "rejected"    # Was: both enums had this
        ERROR = "error"          # Was: ProposalStatus.ERROR
    ```

    Remove:
    - ProposalStatus entirely
    - PolicyStatus duplicates in models.py (import from canonical location)
    - Runtime conversion ProposalStatus(p.status)

    Update all usage to canonical PolicyStatus:
    - persist/__init__.py: import from models/policy_status.py
    - persist/models.py: import from models/policy_status.py
    - persist/sqlite.py: use PolicyStatus.PENDING, PolicyStatus.REJECTED
    - mcp_bridge/servers/approval_policy_bridge.py: use PolicyStatus directly, no conversion

    **Benefits:**
    - Single source of truth
    - Type checker catches misuse
    - Clear lifecycle semantics
    - No runtime conversion needed
    - Easier to understand and maintain
    - Dead code (APPROVED) naturally eliminated

    **Migration note:**
    The string values are compatible, so existing database data doesn't need migration.
    Just update the code to use the unified enum consistently.
  |||,
  properties=['type-safety', 'code-duplication', 'enum-design', 'semantic-clarity', 'maintainability'],
  filesToRanges={
    'adgn/src/adgn/agent/models/proposal_status.py': [
      [6, 10],   // ProposalStatus enum definition (should be removed/unified)
    ],
    'adgn/src/adgn/agent/persist/__init__.py': [
      [54, 58],  // PolicyStatus enum definition (duplicate)
    ],
    'adgn/src/adgn/agent/persist/models.py': [
      [39, 43],  // PolicyStatus enum definition (duplicate, comment says "avoid circular imports")
      [176, 176], // Policy.status typed as PolicyStatus
    ],
    'adgn/src/adgn/agent/persist/sqlite.py': [
      [217, 217], // Creates with ProposalStatus.PENDING (wrong type)
      [231, 231], // Filters with ProposalStatus values (wrong type, includes APPROVED which is never set!)
      [283, 283], // Approves with PolicyStatus.ACTIVE (correct)
      [293, 293], // Rejects with ProposalStatus.REJECTED (wrong type)
    ],
    'adgn/src/adgn/agent/mcp_bridge/servers/approval_policy_bridge.py': [
      [12, 12],  // Imports ProposalStatus
      [76, 76],  // Runtime conversion ProposalStatus(p.status) masks type mismatch
    ],
  },
)
