local I = import '../../specimens/lib.libsonnet';

// iss-031: agent_policy_proposals should return PolicyProposal objects directly, not PolicyProposalInfo wrapper

I.issueOneOccurrence(
  rationale=|||
    The `agent_policy_proposals` function converts `PolicyProposal` objects from the database into
    `PolicyProposalInfo` objects, adding a computable `proposal_uri` field and creating unnecessary
    indirection. It should return the `PolicyProposal` objects directly.

    **Current code:**

    **PolicyProposalInfo model (lines 171-178):**
    ```python
    class PolicyProposalInfo(BaseModel):
        """Policy proposal metadata with URI to full content."""

        id: str
        status: ProposalStatus
        created_at: datetime
        decided_at: datetime | None = None
        proposal_uri: str  # URI to access full proposal content in policy server
    ```

    **Conversion (lines 464-473):**
    ```python
    proposal_infos = [
        PolicyProposalInfo(
            id=p.id,
            status=p.status,
            created_at=p.created_at,
            decided_at=p.decided_at,
            proposal_uri=f"resource://approval-policy/proposals/{p.id}",
        )
        for p in proposals
    ]
    ```

    **Usage (lines 475-477):**
    ```python
    return AgentPolicyProposals(
        agent_id=agent_id, proposals=proposal_infos, active_policy_uri="resource://approval-policy/policy.py"
    )
    ```

    **Where proposals comes from (line 462):**
    ```python
    proposals = await infra.approval_engine.persistence.list_policy_proposals(agent_id)
    ```
    Returns `list[PolicyProposal]` from the persistence layer.

    **Why this is problematic:**

    1. **Unnecessary wrapper type**: `PolicyProposalInfo` is just `PolicyProposal` minus the `content`
       field plus a computable `proposal_uri` field. It's an unnecessary indirection.

    2. **Manual URI construction**: Line 470 manually constructs the URI with an f-string instead of
       using a constant (already flagged in issue 024).

    3. **Redundant data transformation**: Converting from `PolicyProposal` to `PolicyProposalInfo`
       just copies 4 fields and adds a computable URI. This is busywork.

    4. **Multiple sources of truth**: Now there are two types representing proposals:
       - `PolicyProposal` (persistence layer, with content)
       - `PolicyProposalInfo` (API layer, with URI instead of content)

    5. **Inconsistent with codebase**: Other resources return persistence objects directly or use
       the same type throughout.

    **What PolicyProposal looks like (persist/__init__.py:82-87):**
    ```python
    class PolicyProposal(BaseModel):
        id: str
        status: ProposalStatus
        created_at: datetime
        decided_at: datetime | None = None
        content: str
    ```

    **Recommended fix:**

    Delete PolicyProposalInfo entirely and return PolicyProposal objects directly:

    ```python
    # Delete PolicyProposalInfo class (lines 171-178)

    # Update AgentPolicyProposals:
    class AgentPolicyProposals(BaseModel):
        """Content for resource://agents/{id}/policy/proposals."""

        agent_id: AgentID
        proposals: list[PolicyProposal]  # Changed from list[PolicyProposalInfo]
        active_policy_uri: str  # URI to active policy

    # Simplify the function:
    async def agent_policy_proposals(agent_id: AgentID) -> AgentPolicyProposals:
        """Lists policy proposals with full content."""
        infra = await registry.get_infrastructure(agent_id)
        proposals = await infra.approval_engine.persistence.list_policy_proposals(agent_id)

        return AgentPolicyProposals(
            agent_id=agent_id,
            proposals=proposals,  # Direct use - no conversion!
            active_policy_uri="resource://approval-policy/policy.py"
        )
    ```

    **Benefits:**
    - Eliminates unnecessary wrapper type
    - No manual URI construction needed
    - Single source of truth for proposal data
    - Direct use of persistence objects
    - Less code to maintain
    - Clearer data flow (database → API, no transformation)

    **Note:**
    This also resolves issue 024 (proposal_uri field) by removing PolicyProposalInfo entirely.
  |||,
  properties=['unnecessary-abstraction', 'data-modeling', 'simplicity'],
  filesToRanges={
    'adgn/src/adgn/agent/mcp_bridge/servers/agents.py': [
      [171, 178],  // PolicyProposalInfo unnecessary wrapper type
      [464, 473],  // Conversion from PolicyProposal to PolicyProposalInfo
      [470, 470],  // Manual URI construction
      [475, 477],  // Usage in return statement
    ],
    'adgn/src/adgn/agent/persist/__init__.py': [
      [82, 87],    // PolicyProposal original type for reference
    ],
  },
)
