local I = import '../../specimens/lib.libsonnet';

// iss-017: ProposalDetail and PolicyProposal types duplicate each other and should be merged

I.issueOneOccurrence(
  rationale=|||
    The `ProposalDetail` model (server.py:47-54) and `PolicyProposal` model (persist/__init__.py:82-87)
    are duplicates with identical fields. They should be merged into a single type to eliminate redundancy.

    **Current duplication:**

    **ProposalDetail (adgn/src/adgn/mcp/approval_policy/server.py:47-54):**
    ```python
    class ProposalDetail(BaseModel):
        """Full proposal details including content and metadata."""

        id: str
        status: ProposalStatus
        created_at: datetime
        decided_at: datetime | None = None
        content: str
    ```

    **PolicyProposal (adgn/src/adgn/agent/persist/__init__.py:82-87):**
    ```python
    class PolicyProposal(BaseModel):
        id: str
        status: ProposalStatus
        created_at: datetime
        decided_at: datetime | None = None
        content: str
    ```

    **Why this is problematic:**
    - Identical field definitions in two places (5 fields each)
    - Same field types, same defaults, same semantics
    - Changes to proposal structure require updating both types
    - Violates DRY principle
    - Creates confusion about which type to use where
    - PolicyProposal is already defined in the persistence layer (the right place)

    **Recommended fix:**

    1. Delete ProposalDetail from server.py
    2. Import PolicyProposal from adgn.agent.persist in server.py
    3. Replace all uses of ProposalDetail with PolicyProposal:
       - proposal_detail function return type (line 163)
       - ProposalDetail construction (lines 168-174)

    **Updated code:**
    ```python
    # At top of server.py
    from adgn.agent.persist import PolicyProposal

    # ...

    @self.resource(APPROVAL_POLICY_PROPOSALS_INDEX_URI + "/{id}", name="proposal_detail", mime_type="application/json")
    async def proposal_detail(id: str) -> PolicyProposal:
        """Get full proposal details including content and metadata."""
        if (got := await self._engine.persistence.get_policy_proposal(self._engine.agent_id, id)) is None:
            raise KeyError(id)
        return PolicyProposal(
            id=got.id,
            status=ProposalStatus(got.status),
            created_at=got.created_at,
            decided_at=got.decided_at,
            content=got.content,
        )
    ```

    **Benefits:**
    - Single source of truth for proposal type
    - Eliminates 8 lines of duplicate code
    - Clearer type hierarchy (persistence types used by API layer)
    - Changes to proposal structure happen in one place
    - Consistent with how other persistence types are reused

    **Note:**
    ProposalDescriptor (server.py:40-44) is different from PolicyProposal - it's a lightweight
    descriptor WITHOUT content field, so it should remain separate.
  |||,
  properties=['dry-principle', 'duplication', 'type-design'],
  filesToRanges={
    'adgn/src/adgn/mcp/approval_policy/server.py': [
      [47, 54],    // ProposalDetail duplicate type definition
      [163, 174],  // ProposalDetail usage (return type and construction)
    ],
    'adgn/src/adgn/agent/persist/__init__.py': [
      [82, 87],    // PolicyProposal original type definition
    ],
  },
)
