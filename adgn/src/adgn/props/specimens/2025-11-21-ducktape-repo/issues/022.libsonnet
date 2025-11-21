local I = import '../../specimens/lib.libsonnet';

// iss-022: AgentInfo URI fields can be computed from agent_id and should be removed

I.issueOneOccurrence(
  rationale=|||
    The `state_uri`, `approvals_uri`, and `policy_proposals_uri` fields in `AgentInfo` can always be
    computed from just the `agent_id`. These fields should not be exposed in the Pydantic model as they
    add no information and create unnecessary redundancy.

    **Current code (lines 144-146):**
    ```python
    class AgentInfo(BaseModel):
        """Information about a single agent."""

        agent_id: AgentID
        capabilities: dict[str, bool]  # e.g., {"chat": True, "agent_loop": False}
        mode: AgentMode
        state_uri: str | None = None
        approvals_uri: str | None = None
        policy_proposals_uri: str | None = None
    ```

    **Why this is problematic:**

    1. **Computable from agent_id**: These URIs follow deterministic patterns:
       - `state_uri` = `resource://agents/{agent_id}/policy/state`
       - `approvals_uri` = `resource://agents/{agent_id}/approvals/history`
       - `policy_proposals_uri` = `resource://agents/{agent_id}/policy/proposals`

       The client can easily construct these URIs given the agent_id.

    2. **Redundant information**: Storing precomputed values that can be derived from existing data
       violates DRY and creates maintenance burden.

    3. **Inconsistency risk**: If URI patterns change, must update both the construction logic AND
       these field values. Easy to forget updating one.

    4. **Nullable for no reason**: All three are `str | None = None`, but they could always be computed.
       The `None` default suggests they're sometimes not available, which is misleading.

    5. **Not being used**: These fields appear to be defined but not actually populated anywhere
       (no assignments found in codebase), making them dead weight.

    6. **Bloats response payloads**: Including redundant URIs in every AgentInfo response wastes
       bandwidth and makes responses harder to read.

    **Recommended fix:**

    Remove all three URI fields from AgentInfo:

    ```python
    class AgentInfo(BaseModel):
        """Information about a single agent."""

        agent_id: AgentID
        capabilities: dict[str, bool]  # e.g., {"chat": True, "agent_loop": False}
        mode: AgentMode
    ```

    **If URIs are needed by clients**, they should:
    - Construct them client-side from `agent_id` using a simple helper function
    - Or use a separate endpoint/function that returns URIs for an agent

    **Alternative (if URIs must be in response):**
    Add a `@property` or method that computes them on-demand, rather than storing them:
    ```python
    @property
    def state_uri(self) -> str:
        return f"resource://agents/{self.agent_id}/policy/state"
    ```

    But the preferred solution is to remove them entirely and let clients construct URIs.

    **Benefits:**
    - Single source of truth for URI patterns
    - Smaller, cleaner data model
    - No risk of stale/inconsistent URIs
    - Less code to maintain
    - Clearer that URIs are derived, not stored data

    **Note:**
    If these fields ARE being populated somewhere that wasn't found in the search, that code should
    also be deleted as part of this fix.
  |||,
  properties=['redundancy', 'data-modeling', 'dry-principle'],
  filesToRanges={
    'adgn/src/adgn/agent/mcp_bridge/servers/agents.py': [
      [144, 146],  // Redundant URI fields in AgentInfo
    ],
  },
)
