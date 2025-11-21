local I = import '../../specimens/lib.libsonnet';

// iss-009: _policy_id maintains invented counter instead of tracking actual database ID

I.issueOneOccurrence(
  rationale=|||
    The `ApprovalPolicyEngine._policy_id` field maintains a separate invented counter
    instead of tracking the ACTUAL policy ID from the database. This creates two conflicting
    notions of "policy ID" which is confusing and dangerous.

    **Current problematic code:**

    Line 153: `self._policy_id: int = 1  # Start at 1 since we have default content`

    Lines 172-180 (set_policy):
    ```python
    def set_policy(self, source: str) -> int:
        self._policy_source = source
        self._policy_id += 1  # Just increments in-memory counter!
        if self._notify:
            self._notify(APPROVAL_POLICY_RESOURCE_URI)
        return self._policy_id
    ```

    **The problem:**
    1. **Invented IDs**: Line 175 just increments `self._policy_id` without any persistence
    2. **Not the real ID**: The persistence layer has ACTUAL database IDs (sqlite.py:217)
    3. **Confusing name**: Called `_policy_id` but comment says it's for "resource versions"
    4. **Divergence**: In-memory counter diverges from actual database IDs
    5. **No persistence call**: `set_policy()` doesn't call `persistence.set_policy()`

    **What persistence layer ACTUALLY does (sqlite.py:198-217):**
    ```python
    async def set_policy(self, agent_id: AgentID, *, content: str) -> int:
        """Persist a new ACTIVE policy; supersedes existing ACTIVE policy; returns assigned id."""
        async with self._session() as session:
            # Mark existing ACTIVE policy as SUPERSEDED
            await session.execute(
                update(Policy)
                .where(Policy.agent_id == agent_id, Policy.status == PolicyStatus.ACTIVE)
                .values(status=PolicyStatus.SUPERSEDED)
            )
            policy = Policy(
                agent_id=agent_id,
                content=content,
                status=PolicyStatus.ACTIVE,
                created_at=_now(),
                decided_at=_now(),
            )
            session.add(policy)
            await session.commit()
            await session.refresh(policy)
            return policy.id  # Returns ACTUAL database ID
    ```

    **The persistence layer already does the right thing:**
    - Immutable edits: deactivate current (SUPERSEDED), create new (ACTIVE)
    - Returns actual database-assigned ID
    - Never reuses IDs - each policy gets unique auto-increment ID

    **Correct approach:**

    1. **Make `set_policy` async and call persistence:**
    ```python
    async def set_policy(self, source: str) -> int:
        """Store new policy and return its database ID."""
        self._policy_source = source
        # Call persistence to get ACTUAL ID
        self._policy_id = await self.persistence.set_policy(self.agent_id, content=source)
        if self._notify:
            self._notify(APPROVAL_POLICY_RESOURCE_URI)
            self._notify(AGENTS_POLICY_STATE_URI_FMT.format(agent_id=self.agent_id))
        return self._policy_id
    ```

    2. **Initialize from persistence on startup:**
    The `load_policy` method (line 183) already does this correctly:
    ```python
    def load_policy(self, source: str, *, policy_id: int) -> None:
        self._policy_source = source
        self._policy_id = policy_id  # Use actual DB ID
    ```

    3. **Rename if it's meant for versioning:**
    If `_policy_id` is truly meant for MCP resource versioning (not identity), rename it:
    ```python
    self._policy_version: int = ...
    ```
    But this is WRONG - IDs should be actual database IDs, not invented counters.

    **Why this matters:**
    - **Data integrity**: Invented IDs don't correspond to actual persisted policies
    - **Debugging**: Logs/traces will show mismatched IDs
    - **MCP resources**: Resources should be identified by actual database IDs
    - **Immutability**: Policy edits should create new records, not mutate in-memory counter
    - **Consistency**: Every component should use the same ID for the same policy

    **Current behavior is wrong:**
    If you:
    1. Load policy ID 5 from database (via `load_policy`)
    2. Call `set_policy()` twice
    3. `_policy_id` becomes 7 (5+1+1) but database has IDs 6 and 7

    **Related issues:**
    - Line 175 comment says "resource versions for the MCP protocol" but that's what the ID should be
    - If MCP needs version numbers separate from IDs, create a separate `_policy_version` field
  |||,
  properties=['data-integrity', 'persistence', 'immutability', 'correctness'],
  filesToRanges={
    'adgn/src/adgn/agent/approvals.py': [
      [150, 153],  // Comments and _policy_id initialization
      [172, 180],  // set_policy incrementing instead of persisting
      [183, 186],  // load_policy (does use actual ID - correct)
    ],
    'adgn/src/adgn/agent/persist/sqlite.py': [
      [198, 217],  // Persistence set_policy that returns ACTUAL ID
    ],
  },
)
