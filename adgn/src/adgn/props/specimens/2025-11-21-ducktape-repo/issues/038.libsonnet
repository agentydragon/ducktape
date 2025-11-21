local I = import '../../specimens/lib.libsonnet';

// iss-038: Approval policy URIs are global but should be agent-scoped

I.issueOneOccurrence(
  rationale=|||
    The approval policy resource URIs use a global namespace (`resource://approval-policy/...`)
    but should be scoped per agent like all other agent resources (`resource://agents/<id>/...`).

    **Current global URIs:**

    ```python
    APPROVAL_POLICY_RESOURCE_URI = "resource://approval-policy/policy.py"
    APPROVAL_POLICY_PROPOSALS_INDEX_URI = "resource://approval-policy/proposals"
    ```

    **Agent-scoped URIs for comparison:**

    All other agent resources are properly scoped:
    - `resource://agents/{agent_id}/approvals/pending`
    - `resource://agents/{agent_id}/approvals/history`
    - `resource://agents/{agent_id}/approvals/{call_id}`
    - `resource://agents/{agent_id}/policy/proposals`
    - `resource://agents/{agent_id}/policy/state`
    - `resource://agents/{agent_id}/session/state`
    - `resource://agents/{agent_id}/ui/state`
    - `resource://agents/{agent_id}/mcp/state`

    **Why this is problematic:**

    1. **Architectural inconsistency**: Every agent has its own approval policy server (documented
       as "per-agent server"), so the URIs should be scoped per agent.

    2. **Existing per-agent policy URIs**: The agents MCP server already exposes:
       - `resource://agents/{agent_id}/policy/proposals`
       - `resource://agents/{agent_id}/policy/state`

       But the approval policy server itself uses global URIs. This creates duplication and confusion
       about which URI to use.

    3. **Multi-agent ambiguity**: In a system with multiple agents, global URIs like
       `resource://approval-policy/policy.py` don't indicate which agent's policy is being referenced.

    4. **Mixed notifications**: When policy changes, the code notifies BOTH URIs:
       ```python
       # approvals.py:178-181
       if self._notify:
           self._notify(APPROVAL_POLICY_RESOURCE_URI)  # Global: resource://approval-policy/policy.py
           self._notify(AGENTS_POLICY_STATE_URI_FMT.format(agent_id=self.agent_id))  # Agent-scoped
       ```
       This is redundant and confusing.

    5. **URI construction inconsistency**: Some places manually construct proposal URIs, others
       use helper functions, but all use the global namespace:
       ```python
       # resources.py:67-69
       def policy_proposal(proposal_id: str) -> str:
           return f"resource://approval-policy/proposals/{proposal_id}"

       # agents.py:470
       proposal_uri=f"resource://approval-policy/proposals/{p.id}"
       ```

    **What should be done:**

    Replace global approval policy URIs with agent-scoped URIs:

    **Before:**
    ```python
    APPROVAL_POLICY_RESOURCE_URI = "resource://approval-policy/policy.py"
    APPROVAL_POLICY_PROPOSALS_INDEX_URI = "resource://approval-policy/proposals"

    def policy_proposal(proposal_id: str) -> str:
        return f"resource://approval-policy/proposals/{proposal_id}"
    ```

    **After:**
    ```python
    def approval_policy(agent_id: AgentID) -> str:
        """Resource URI for active approval policy."""
        return f"resource://agents/{agent_id}/approval-policy/policy.py"

    def approval_policy_proposals_index(agent_id: AgentID) -> str:
        """Resource URI for approval policy proposals index."""
        return f"resource://agents/{agent_id}/approval-policy/proposals"

    def approval_policy_proposal(agent_id: AgentID, proposal_id: str) -> str:
        """Resource URI for a specific approval policy proposal."""
        return f"resource://agents/{agent_id}/approval-policy/proposals/{proposal_id}"
    ```

    **Update sites:**
    - Constants definition in `mcp/_shared/constants.py` (lines 47-48)
    - Helper functions in `agent/mcp_bridge/resources.py` (lines 12, 67-69)
    - Notification calls in `agent/approvals.py` (line 179 - remove, keep only line 181)
    - Manual URI construction in `agent/mcp_bridge/servers/agents.py` (line 470)
    - MCP resource registration in `mcp/approval_policy/server.py` (lines 132, 142, 148, 161)
    - All tests and documentation

    **Benefits:**
    - Consistent URI namespace (all agent resources under `resource://agents/{id}/...`)
    - Clear ownership (URI clearly indicates which agent's policy)
    - No redundant notifications (single agent-scoped URI instead of two)
    - Easier to understand and maintain
    - Matches the documented "per-agent server" architecture

    **Note:**
    This change also eliminates the need for the redundant agent-specific policy URIs that
    currently exist separately (`AGENTS_POLICY_PROPOSALS_URI_FMT`, `AGENTS_POLICY_STATE_URI_FMT`),
    as the approval policy server URIs will directly match the agent resource namespace.
  |||,
  properties=['architectural-design', 'uri-structure', 'consistency'],
  filesToRanges={
    'adgn/src/adgn/mcp/_shared/constants.py': [
      [47, 48],   // Global APPROVAL_POLICY_RESOURCE_URI and PROPOSALS_INDEX_URI constants
    ],
    'adgn/src/adgn/agent/mcp_bridge/resources.py': [
      [12, 12],   // ACTIVE_POLICY global constant
      [67, 69],   // policy_proposal() helper uses global namespace
    ],
    'adgn/src/adgn/agent/approvals.py': [
      [178, 181], // Notifies both global and agent-scoped URIs
    ],
    'adgn/src/adgn/agent/mcp_bridge/servers/agents.py': [
      [470, 470], // Manual URI construction using global namespace
    ],
    'adgn/src/adgn/mcp/approval_policy/server.py': [
      [132, 132], // Resource registration using global APPROVAL_POLICY_PROPOSALS_INDEX_URI
      [142, 142], // Resource registration using global APPROVAL_POLICY_RESOURCE_URI
      [148, 148], // Resource registration using global proposals index + "/list"
      [161, 161], // Resource registration using global proposals index + "/{id}"
    ],
  },
)
